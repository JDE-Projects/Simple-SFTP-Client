"""Check that code moved out of simple_sftp_client.py arrived unchanged.

Compares every top-level definition (each method separately, for classes) and
every comment in the current simple_sftp_client.py plus app/ against the
original simple_sftp_client.py at a git revision (default: main). Reading a
setting as `constants.X` or `paths.X` counts as reading `X`, and a service
function's first argument named `api` counts as `self`, since those are the
only edits a move allows. app/debug_log.py and app/transfer_queue.py are
skipped: they moved whole and are compared as files.

Usage:
    .venv/Scripts/python.exe tools/verify_move.py [revision]

Prints each missing or changed definition and each lost comment, then a
count. Exit code 1 when anything is missing or changed.
"""
import ast
import io
import subprocess
import sys
import tokenize
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKIP = {"debug_log.py", "transfer_queue.py"}
QUAL = {"constants", "paths"}


class Norm(ast.NodeTransformer):
    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.value, ast.Name) and node.value.id in QUAL:
            return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
        return node

    def visit_Name(self, node):
        if node.id == "api":
            node.id = "self"
        return node

    def visit_arg(self, node):
        if node.arg == "api":
            node.arg = "self"
        return node


def defs(src):
    out = {}
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        if isinstance(node, ast.ClassDef):
            # Compare classes member by member so a change names its method.
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{node.name}.{m.name}"] = ast.dump(Norm().visit(m))
            node.body = [m for m in node.body
                         if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
        dump = ast.dump(Norm().visit(node))
        for n in names:
            out[n] = dump
    return out


def comments(src):
    toks = tokenize.generate_tokens(io.StringIO(src).readline)
    return Counter(t.string.strip() for t in toks if t.type == tokenize.COMMENT)


def main():
    revision = sys.argv[1] if len(sys.argv) > 1 else "main"
    orig_src = subprocess.run(
        ["git", "show", f"{revision}:simple_sftp_client.py"], cwd=REPO,
        capture_output=True, text=True, encoding="utf-8", check=True).stdout

    new_files = [REPO / "simple_sftp_client.py"] + [
        p for p in sorted((REPO / "app").rglob("*.py")) if p.name not in SKIP]
    new_defs, new_comments, where = {}, Counter(), {}
    for p in new_files:
        src = p.read_text(encoding="utf-8")
        for name, dump in defs(src).items():
            if name in new_defs and new_defs[name] != dump:
                print(f"DUPLICATE differing definition: {name} in "
                      f"{p.relative_to(REPO).as_posix()} and {where[name]}")
            new_defs[name] = dump
            where[name] = p.relative_to(REPO).as_posix()
        new_comments += comments(src)

    orig_defs = defs(orig_src)
    problems = 0
    for name, dump in orig_defs.items():
        if name not in new_defs:
            print(f"MISSING definition: {name}")
            problems += 1
        elif new_defs[name] != dump:
            print(f"CHANGED definition: {name} ({where[name]})")
            problems += 1
    orig_comments = comments(orig_src)
    for c, n in (orig_comments - new_comments).items():
        print(f"LOST comment x{n}: {c[:100]}")
        problems += 1
    for c, n in (new_comments - orig_comments).items():
        print(f"NEW comment x{n}: {c[:100]}")
    print(f"checked {len(orig_defs)} definitions and {sum(orig_comments.values())} "
          f"comments against {revision}; problems: {problems}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
