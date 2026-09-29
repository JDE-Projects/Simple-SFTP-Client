"""Check that code moved out of simple_sftp_client.py arrived unchanged.

Compares every top-level definition (each method separately, for classes) and
every comment in the current simple_sftp_client.py plus app/ against the
original simple_sftp_client.py at a git revision (default: main). Reading a
setting as `constants.X` or `paths.X` counts as reading `X`, and a service
function's first argument named `api` counts as `self`, since those are the
only edits a move allows. Docstrings are compared after removing their
indentation, which changes when a method body leaves its class.
app/debug_log.py and app/transfer_queue.py are skipped: they moved whole and
are compared as files.

Api methods:
- A function in app/services/ whose name is an original Api method is that
  method's body, compared without decorators.
- Every original Api method must still exist on class Api with the original
  decorators, argument names, and defaults. Its body is either the original
  body or exactly `return services.<module>.<name>(self, <arguments>)`
  (no `self` for a staticmethod).
- Comments inside a method are checked against that method's original
  comments; comments outside any function are checked as one pool.

INTENDED lists the few deliberate edits. Each is applied to the original
before comparing, so the new code must match the edited original exactly.

Usage:
    .venv/Scripts/python.exe tools/verify_move.py [revision]

Prints each missing or changed definition and each lost comment, then a
count. Exit code 1 when anything is missing or changed.
"""
import ast
import copy
import inspect
import io
import subprocess
import sys
import tokenize
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKIP = {"debug_log.py", "transfer_queue.py"}
QUAL = {"constants", "paths"}
SERVICES = REPO / "app" / "services"


def _clean_doc(node):
    body = getattr(node, "body", None)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body[0].value.value = inspect.cleandoc(body[0].value.value)


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

    def _doc(self, node):
        _clean_doc(node)
        self.generic_visit(node)
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _doc


# Deliberate edits, applied to the original definition before comparing.

class _UpOneFolder(ast.NodeTransformer):
    # app/paths.py sits one folder below the project root.
    def visit_Call(self, node):
        self.generic_visit(node)
        if (ast.unparse(node.func) == "os.path.dirname" and len(node.args) == 1
                and ast.unparse(node.args[0]) == "os.path.abspath(__file__)"):
            return ast.Call(func=node.func, args=[node], keywords=[])
        return node


class _VersionFromBridge(ast.NodeTransformer):
    # The launcher hands the version to Api; app/ never imports it.
    def visit_Name(self, node):
        if node.id == "APP_VERSION":
            return ast.Attribute(value=ast.Name(id="self", ctx=ast.Load()),
                                 attr="_app_version", ctx=node.ctx)
        return node


class _VersionIntoBridge(ast.NodeTransformer):
    # The launcher creates the bridge as Api(APP_VERSION).
    def visit_Call(self, node):
        self.generic_visit(node)
        if ast.unparse(node) == "Api()":
            node.args = [ast.Name(id="APP_VERSION", ctx=ast.Load())]
        return node


def _version_init(node):
    node.args.args.insert(1, ast.arg(arg="app_version"))
    node.body.insert(0, ast.parse("self._app_version = app_version").body[0])
    return node


INTENDED = {
    "resource_path": lambda n: _UpOneFolder().visit(n),
    "exe_dir": lambda n: _UpOneFolder().visit(n),
    "main": lambda n: _VersionIntoBridge().visit(n),
    "Api.__init__": _version_init,
    "Api.get_meta": lambda n: _VersionFromBridge().visit(n),
    "Api.check_update": lambda n: _VersionFromBridge().visit(n),
}


def _span(node):
    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
    return start, node.end_lineno


def defs(src, service=False, api_names=()):
    """Return {name: node}, {name: (first line, last line)}, and the Api class
    node if this source defines one. Class methods are keyed Class.method;
    in a service module, functions named after an Api method are keyed
    Api.name."""
    out, spans, api_cls = {}, {}, None
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
            if node.name == "Api":
                api_cls = node
            # Compare classes member by member so a change names its method.
            # An Api hand-off is checked by check_api_class; its body is the
            # service function.
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    spans[f"{node.name}.{m.name}"] = _span(m)
                    if not (node.name == "Api" and handoff_target(m)):
                        out[f"{node.name}.{m.name}"] = m
            node = copy.deepcopy(node)
            node.body = [m for m in node.body
                         if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
        elif service and isinstance(node, ast.FunctionDef) and node.name in api_names:
            names = [f"Api.{node.name}"]
            spans[names[0]] = _span(node)
        elif isinstance(node, ast.FunctionDef):
            spans[node.name] = _span(node)
        for n in names:
            out[n] = node
    return out, spans, api_cls


def dump(node, strip_decorators=False):
    node = Norm().visit(copy.deepcopy(node))
    if strip_decorators and hasattr(node, "decorator_list"):
        node.decorator_list = []
    return ast.dump(node)


def comments(src, spans):
    """Comments inside each function span, plus a pool for the rest."""
    per, pool = {}, Counter()
    toks = tokenize.generate_tokens(io.StringIO(src).readline)
    for t in toks:
        if t.type != tokenize.COMMENT:
            continue
        line = t.start[0]
        owner = [k for k, (a, b) in spans.items() if a <= line <= b]
        # Innermost span wins (a method inside its class).
        owner.sort(key=lambda k: spans[k][1] - spans[k][0])
        if owner:
            per.setdefault(owner[0], Counter())[t.string.strip()] += 1
        else:
            pool[t.string.strip()] += 1
    return per, pool


def handoff_target(m):
    """(module, name, argument names) for a one-line hand-off, else None."""
    if len(m.body) != 1 or not isinstance(m.body[0], ast.Return):
        return None
    call = m.body[0].value
    if not (isinstance(call, ast.Call) and not call.keywords
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Attribute)
            and isinstance(call.func.value.value, ast.Name)
            and call.func.value.value.id == "services"
            and all(isinstance(a, ast.Name) for a in call.args)):
        return None
    return call.func.value.attr, call.func.attr, [a.id for a in call.args]


def check_api_class(api_cls, orig_defs, where):
    """Every original Api method is on the new class with its original
    signature and decorators; hand-offs pass their arguments straight
    through. Returns (problem count, names that are hand-offs)."""
    problems, handoffs = 0, set()
    members = {m.name: m for m in api_cls.body
               if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for key, orig in orig_defs.items():
        if not key.startswith("Api."):
            continue
        name = key[4:]
        m = members.get(name)
        if m is None:
            print(f"MISSING from class Api: {name}")
            problems += 1
            continue
        if key in INTENDED:
            orig = INTENDED[key](copy.deepcopy(orig))
        if ([ast.dump(d) for d in m.decorator_list]
                != [ast.dump(d) for d in orig.decorator_list]):
            print(f"CHANGED decorators: Api.{name} ({where})")
            problems += 1
        if ast.dump(Norm().visit(copy.deepcopy(m.args)))                 != ast.dump(Norm().visit(copy.deepcopy(orig.args))) \
                or ast.dump(m.returns or ast.Constant(None)) \
                != ast.dump(orig.returns or ast.Constant(None)):
            print(f"CHANGED signature: Api.{name} ({where})")
            problems += 1
        target = handoff_target(m)
        if target is None:
            continue
        _mod, fn, passed = target
        params = [a.arg for a in m.args.posonlyargs + m.args.args]
        if m.args.vararg or m.args.kwarg or m.args.kwonlyargs:
            print(f"BAD hand-off (unsupported argument kinds): Api.{name}")
            problems += 1
        elif fn != name or passed != params:
            print(f"BAD hand-off: Api.{name} calls {fn}({', '.join(passed)}), "
                  f"expected {name}({', '.join(params)})")
            problems += 1
        handoffs.add(key)
    return problems, handoffs


def main():
    revision = sys.argv[1] if len(sys.argv) > 1 else "main"
    orig_src = subprocess.run(
        ["git", "show", f"{revision}:simple_sftp_client.py"], cwd=REPO,
        capture_output=True, text=True, encoding="utf-8", check=True).stdout
    orig_defs, orig_spans, _ = defs(orig_src)
    api_names = {k[4:] for k in orig_defs if k.startswith("Api.")}

    new_files = [REPO / "simple_sftp_client.py"] + [
        p for p in sorted((REPO / "app").rglob("*.py")) if p.name not in SKIP]
    new_defs, where, api_cls, api_where = {}, {}, None, None
    new_comments, new_pool, api_comments = {}, Counter(), {}
    problems = 0
    for p in new_files:
        src = p.read_text(encoding="utf-8")
        rel = p.relative_to(REPO).as_posix()
        service = SERVICES in p.parents
        found, spans, cls = defs(src, service, api_names)
        if cls is not None:
            if api_cls is not None:
                print(f"DUPLICATE class Api in {rel} and {api_where}")
                problems += 1
            api_cls, api_where = cls, rel
            api_comments = comments(src, spans)[0]
        for name, node in found.items():
            if name in new_defs:
                print(f"DUPLICATE definition: {name} in {rel} and {where[name]}")
                problems += 1
            new_defs[name] = node
            where[name] = rel
        per, pool = comments(src, spans)
        for k, c in per.items():
            new_comments.setdefault(k, Counter()).update(c)
        new_pool += pool

    handoffs = set()
    if api_cls is None:
        print("MISSING class Api")
        problems += 1
    else:
        n, handoffs = check_api_class(api_cls, orig_defs, api_where)
        problems += n
        for key in sorted(handoffs):
            if api_comments.get(key):
                print(f"BAD hand-off (carries comments): {key}")
                problems += 1

    for name, orig in orig_defs.items():
        is_api = name.startswith("Api.")
        if name in INTENDED:
            before = dump(orig, is_api)
            orig = INTENDED[name](copy.deepcopy(orig))
            if dump(orig, is_api) == before:
                print(f"INTENDED edit for {name} no longer matches the original")
                problems += 1
        if name not in new_defs:
            print(f"MISSING definition: {name}")
            problems += 1
        elif dump(new_defs[name], is_api) != dump(orig, is_api):
            print(f"CHANGED definition: {name} ({where[name]})")
            problems += 1

    orig_comments, orig_pool = comments(orig_src, orig_spans)
    for key in sorted(set(orig_comments) | set(new_comments)):
        old, new = orig_comments.get(key, Counter()), new_comments.get(key, Counter())
        for c, n in (old - new).items():
            print(f"LOST comment x{n} in {key}: {c[:100]}")
            problems += 1
        for c, n in (new - old).items():
            print(f"NEW comment x{n} in {key}: {c[:100]}")
    for c, n in (orig_pool - new_pool).items():
        print(f"LOST comment x{n}: {c[:100]}")
        problems += 1
    for c, n in (new_pool - orig_pool).items():
        print(f"NEW comment x{n}: {c[:100]}")
    total = sum(orig_pool.values()) + sum(sum(c.values()) for c in orig_comments.values())
    print(f"checked {len(orig_defs)} definitions ({len(handoffs)} Api hand-offs) and "
          f"{total} comments against {revision}; problems: {problems}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
