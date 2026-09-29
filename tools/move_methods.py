"""Move one Api method area into a service module; it only supports planned plain methods."""

import ast
import builtins
import io
import subprocess
import sys
import tokenize
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
API_PATH = REPO / "app" / "api.py"
SERVICES = REPO / "app" / "services"
AREAS = {
    "updates": "check_update _is_newer open_url".split(),
    "window": "set_window get_meta get_theme save_theme set_debug _on_debug_warning _drain_debug_warnings drain_debug_warnings export_console _emit _vlog _worker_log shutdown confirm_quit".split(),
    "keys": "default_key_path browse_save_key generate_key install_pubkey".split(),
    "sessions": "_load_sessions _sessions_notice _save_sessions save_session delete_session _remembered_password get_remembered".split(),
    "connections": "_open _close_partial connect _sweep_scratch_files trust_host_key get_host_key _transport_info test_connection disconnect _connection_dead _report_dead_connection".split(),
    "browsing": "ping list_local list_remote make_dir rename delete _rremove open_local calc_remote_size browse_folder".split(),
    "transfers": "cancel poll_queue cancel_item clear_finished retry_item retry_all_failed pause_queue resume_queue enqueue _enqueue_files _ensure_worker _worker_loop upload_paths _normalize_drop_path on_external_drop _one _progress _rstat _transfers_active transfers_active _make_dir _ensure_remote_dir".split(),
    "transfer_io": "_put_resume _get_resume _mtime_fallback_key _record_mtime_fallback _clear_mtime_fallback _mtime_fallback_matches _apply_download_mtime _apply_upload_mtime".split(),
    "scanning": "_register_scan _deregister_scan _stop_all_scans _scan_active _bump_scan_found _scan_found_total _scan_wait_for_room _iter_local _iter_remote _scan_and_queue".split(),
    "compare_sync": "_compute_pair_maps _classify _compute_compare _compute_sync _start_compare _deregister_compare _stop_all_compares _compare_active _bump_compare_found _compare_found_total _run_compare compare sync_plan _stream_sync_transfers start_sync discard_sync".split(),
    "watching": "start_watch stop_watch".split(),
}


def fail(message):
    raise ValueError(message)


def lines_for(source, node):
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1:node.end_lineno])


def header_for(source, node):
    """The method's `def` line through the line holding the signature's
    closing colon (the first `:` outside brackets)."""
    text = lines_for(source, node)
    depth = 0
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type != tokenize.OP:
            continue
        if token.string in "([{":
            depth += 1
        elif token.string in ")]}":
            depth -= 1
        elif token.string == ":" and depth == 0:
            return "".join(text.splitlines(keepends=True)[:token.start[0]])
    fail(f"cannot find signature end for {node.name}")


def plain_method(node):
    if not isinstance(node, ast.FunctionDef):
        fail(f"{getattr(node, 'name', '<unknown>')} is not a plain method")
    args = node.args
    if args.vararg or args.kwarg or args.kwonlyargs:
        fail(f"{node.name} has unsupported argument kinds")


def check_method(source, node):
    plain_method(node)
    text = lines_for(source, node)
    tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    previous = None
    for token in tokens:
        if token.type == tokenize.NAME and token.string == "api":
            fail(f"{node.name} already uses the name api")
        if token.type == tokenize.NAME and token.string == "self":
            if previous and previous.string == ".":
                fail(f"{node.name} has self after a dot")
            # NAME followed by = is a keyword argument name.
            position = tokens.index(token)
            if position + 1 < len(tokens) and tokens[position + 1].string == "=":
                fail(f"{node.name} has self as a keyword argument name")
        previous = token
    docstring = node.body[0] if (node.body and isinstance(node.body[0], ast.Expr)
                                 and isinstance(node.body[0].value, ast.Constant)
                                 and isinstance(node.body[0].value.value, str)) else None
    for token in tokens:
        literal = token.string.lower().lstrip("rubf")
        triple = literal.startswith("'''" ) or literal.startswith('\"\"\"')
        is_docstring = docstring and token.start[0] == docstring.lineno - node.lineno + 1
        if token.type == tokenize.STRING and triple and token.start[0] != token.end[0] and not is_docstring:
            fail(f"{node.name} has a multiline non-docstring triple-quoted string")


def rename_self(text):
    """Replace each NAME token `self` with `api` in place, leaving every
    other character (spacing, comments, strings, continuations) as it was."""
    lines = text.splitlines(keepends=True)
    hits = [token.start for token in tokenize.generate_tokens(io.StringIO(text).readline)
            if token.type == tokenize.NAME and token.string == "self"]
    for row, col in reversed(hits):
        line = lines[row - 1]
        lines[row - 1] = line[:col] + "api" + line[col + 4:]
    return "".join(lines)


def dedent_four(text):
    out = []
    for line in text.splitlines(keepends=True):
        if not line.strip():
            out.append(line)
        elif line.startswith("    "):
            out.append(line[4:])
        else:
            fail("a line is indented less than the method body: " + line.rstrip()[:60])
    return "".join(out)


def module_bindings(tree, source):
    """{bound name: (index of its import statement, import form)}. The form is
    `import x` for a plain import, or the module for `from module import`."""
    bindings = {}
    for index, node in enumerate(tree.body):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    fail("aliased imports are not supported: " + ast.unparse(node))
                bindings[alias.name.split(".")[0]] = (index, ast.unparse(node))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.asname:
                    fail("aliased imports are not supported: " + ast.unparse(node))
                bindings[alias.name] = (index, node.module)
    return bindings


def global_names(text):
    table = __import__("symtable").symtable(text, "<service>", "exec")
    names = set()
    def walk(current):
        for name in current.get_identifiers():
            symbol = current.lookup(name)
            if symbol.is_global() and symbol.is_referenced():
                names.add(name)
        for child in current.get_children():
            walk(child)
    walk(table)
    return names


def service_imports(bindings, bodies):
    needed = set()
    for body in bodies:
        needed.update(global_names(body))
    missing = sorted(name for name in needed
                     if name not in bindings and name not in dir(builtins)
                     and name not in {"__name__"})
    if missing:
        fail("used names have no app/api.py binding: " + ", ".join(missing))
    # Same order as app/api.py; a `from` import keeps only the names used.
    statements = {}
    for name, (index, form) in bindings.items():
        if name in needed:
            statements.setdefault((index, form), []).append(name)
    imports = []
    for (_index, form), names in sorted(statements.items()):
        if form.startswith("import "):
            imports.append(form)
        else:
            imports.append(f"from {form} import {', '.join(names)}")
    return "\n".join(imports)


def handoff(node):
    parameters = [arg.arg for arg in node.args.posonlyargs + node.args.args]
    static = any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in node.decorator_list)
    passed = parameters if static else ["self"] + parameters[1:]
    return "    " + header_for(API_SOURCE, node).lstrip() + "        return services.%s.%s(%s)\n" % (AREA, node.name, ", ".join(passed))


def rewrite_api(source, nodes):
    lines = source.splitlines(keepends=True)
    replacements = []
    for node in nodes:
        start = node.lineno - 1
        replacements.append((start, node.end_lineno, handoff(node)))
    for start, end, replacement in sorted(replacements, reverse=True):
        lines[start:end] = [replacement]
    text = "".join(lines)
    if "from app import services\n" not in text:
        anchor = "from app import constants, paths\n"
        if text.count(anchor) != 1:
            fail("cannot find where to add `from app import services`")
        text = text.replace(anchor, anchor + "from app import services\n")
    return text


def main():
    global API_SOURCE, AREA
    if len(sys.argv) != 2:
        fail("usage: .venv/Scripts/python.exe tools/move_methods.py <area>")
    AREA = sys.argv[1]
    if AREA not in AREAS:
        fail(f"unknown area: {AREA}")
    service_path = SERVICES / f"{AREA}.py"
    if service_path.exists():
        fail(f"area already moved: {AREA}")
    API_SOURCE = API_PATH.read_text(encoding="utf-8")
    tree = ast.parse(API_SOURCE)
    api = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Api"), None)
    if api is None:
        fail("app/api.py has no class Api")
    methods = {node.name: node for node in api.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    nodes = []
    for name in AREAS[AREA]:
        node = methods.get(name)
        if node is None:
            fail(f"missing method: {name}")
        check_method(API_SOURCE, node)
        nodes.append(node)
    bindings = module_bindings(tree, API_SOURCE)
    bodies = [dedent_four(rename_self(lines_for(API_SOURCE, node))) for node in nodes]
    imports = service_imports(bindings, bodies)
    service = f'"""Service functions for the {AREA} area."""\n'
    if imports:
        service += "\n" + imports + "\n"
    service += "\n\n" + "\n\n\n".join(body.rstrip() for body in bodies) + "\n"
    api_text = rewrite_api(API_SOURCE, nodes)
    existing = sorted(path.stem for path in SERVICES.glob("*.py") if path.name != "__init__.py")
    areas = sorted(existing + [AREA])
    init = ('"""Api service modules."""\n\nfrom app.services import ' + ", ".join(areas)
            + "\n\n__all__ = [" + ", ".join(f'"{a}"' for a in areas) + "]\n")
    # All validation above precedes these writes.
    SERVICES.mkdir(exist_ok=True)
    service_path.write_text(service, encoding="utf-8")
    API_PATH.write_text(api_text, encoding="utf-8")
    (SERVICES / "__init__.py").write_text(init, encoding="utf-8")
    result = subprocess.run([sys.executable, "-m", "ruff", "check", str(API_PATH), "--select", "F401", "--fix"], cwd=REPO)
    if result.returncode:
        fail("ruff could not remove unused app/api.py imports")
    print("moved: " + ", ".join(AREAS[AREA]))
    for path in (API_PATH, service_path, SERVICES / "__init__.py"):
        print(f"{path.relative_to(REPO)}: {len(path.read_text(encoding='utf-8').splitlines())}")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(f"move_methods.py: {error}", file=sys.stderr)
        sys.exit(2)
