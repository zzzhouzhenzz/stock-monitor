"""Build public repository and Python symbol metadata for the architecture page."""

import ast
from collections import Counter
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import quote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = "docs/hierarchy-data.js"
PUBLIC_DOCS = {"docs/architecture.html", "docs/build_hierarchy.py", "docs/test_build_hierarchy.py", OUTPUT}
PRIVATE_PARTS = {".git", ".state", ".venv", ".python-runtime", ".aws", ".codex", ".agents", "__pycache__"}


def git(*arguments):
    """Read local Git metadata without contacting a remote."""
    return subprocess.check_output(["git", "-C", str(ROOT), *arguments], text=True).strip()


def public_path(path):
    """Exclude private runtime files and symlinks before opening any contents."""
    parts = Path(path).parts
    name = Path(path).name.lower()
    return (not Path(path).is_absolute() and ".." not in parts
            and not PRIVATE_PARTS.intersection(parts)
            and not name.startswith(".env") and ".local." not in name
            and not re.search(r"(^|[._-])(credentials?|secrets?|tokens?)([._-]|$)", name)
            and Path(path).suffix.lower() not in {".pem", ".key", ".p12", ".pfx"}
            and not any((ROOT / Path(*parts[:index])).is_symlink() for index in range(1, len(parts) + 1)))


def summary(node):
    """Use only the first docstring paragraph, with a bounded display length."""
    text = " ".join((ast.get_docstring(node) or "").split("\n\n", 1)[0].split())
    return text if len(text) <= 240 else text[:237] + "..."


def module_name(path):
    parts = Path(path).with_suffix("").parts
    if parts[0] == "src":
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def github_base(ref):
    """Construct links only for a recognized GitHub remote; omit credentials."""
    try:
        remote = git("remote", "get-url", "origin")
    except subprocess.CalledProcessError:
        return ""
    if remote.startswith("git@github.com:"):
        path = remote.split(":", 1)[1]
    else:
        parsed = urlsplit(remote)
        if parsed.hostname != "github.com":
            return ""
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git")
    return f"https://github.com/{path}/blob/{quote(ref, safe='')}/" if re.fullmatch(r"[\w.-]+/[\w.-]+", path) else ""


def build():
    """Read public tracked files and the explicitly named hierarchy docs."""
    tracked = set(git("ls-files", "-z").split("\0")) - {""}
    paths = sorted(path for path in tracked | PUBLIC_DOCS if public_path(path)
                   and ((ROOT / path).is_file() or path == OUTPUT))
    commit = git("rev-parse", "HEAD")
    committed = set(git("ls-tree", "-r", "--name-only", "-z", "HEAD").split("\0")) - {""}
    changed = set(git("diff", "HEAD", "--name-only", "-z").split("\0")) - {""}
    unchanged = committed - changed
    remote = github_base(commit)
    branch = git("branch", "--show-current")
    branch_remote = github_base(branch) if branch else ""
    counts, ids, trees = Counter(), Counter(), {}

    def node(name, kind, path, symbol="", line=None, end=None, signature="", doc=""):
        key = f"{kind}:{path or '.'}" + (f":{symbol}" if symbol else "")
        ids[key] += 1
        result = dict(id=key + (f"@{ids[key]}" if ids[key] > 1 else ""), name=name, kind=kind,
                      path=path, line=line, endLine=end, signature=signature, doc=doc, children=[])
        counts[kind] += 1
        if kind != "directory":
            anchor = f"#L{line}" if line else ""
            result["sourceHref"] = "../" + quote(path, safe="/") + anchor
            if remote and path in unchanged:
                result["sourceUrl"] = remote + quote(path, safe="/") + anchor
            elif branch_remote:
                result["sourceBranchUrl"] = branch_remote + quote(path, safe="/") + anchor
        return result

    def symbols(items, path, scope="", class_body=False):
        children = []
        for item in items:
            if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = f"{scope}.{item.name}" if scope else item.name
                is_class = isinstance(item, ast.ClassDef)
                if is_class:
                    arguments = [ast.unparse(base) for base in item.bases + item.keywords]
                    signature = f"class {item.name}" + (f"({', '.join(arguments)})" if arguments else "")
                else:
                    signature = ("async " if isinstance(item, ast.AsyncFunctionDef) else "")
                    signature += f"def {item.name}({ast.unparse(item.args)})"
                    if item.returns:
                        signature += " -> " + ast.unparse(item.returns)
                entry = node(item.name, "class" if is_class else "function", path, qualified,
                             item.lineno, item.end_lineno, signature, summary(item))
                if not is_class:
                    entry["async"] = isinstance(item, ast.AsyncFunctionDef)
                    counts["asyncFunctions"] += int(entry["async"])
                    counts["testFunctions"] += int(Path(path).name.startswith("test_") and item.name.startswith("test_"))
                entry["children"] = symbols(item.body, path, qualified, is_class)
                children.append(entry)
            elif class_body and isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                name = item.target.id
                children.append(node(name, "field", path, f"{scope}.{name}", item.lineno,
                                     item.end_lineno, f"{name}: {ast.unparse(item.annotation)}"))
            else:
                children.extend(symbols(ast.iter_child_nodes(item), path, scope, class_body))
        return children

    root = node(ROOT.name, "directory", "")
    directories = {"": root}
    for path in paths:
        parent = ""
        for part in Path(path).parts[:-1]:
            current = f"{parent}/{part}" if parent else part
            if current not in directories:
                directories[current] = node(part, "directory", current)
                directories[parent]["children"].append(directories[current])
            parent = current
        entry = node(Path(path).name, "file", path)
        if path.endswith(".py"):
            text = (ROOT / path).read_text(encoding="utf-8")
            trees[path] = ast.parse(text, filename=path)
            entry.update(line=1, endLine=len(text.splitlines()), doc=summary(trees[path]),
                         children=symbols(trees[path].body, path))
            counts["pythonFiles"] += 1
            counts["testFiles"] += int(Path(path).name.startswith("test_"))
        directories[parent]["children"].append(entry)
    for directory in directories.values():
        directory["children"].sort(key=lambda child: (child["kind"] != "directory", child["name"]))

    modules = {module_name(path): path for path in trees}
    imports = set()
    for path, tree in trees.items():
        package = module_name(path) if path.endswith("/__init__.py") else module_name(path).rpartition(".")[0]
        for item in ast.walk(tree):
            targets = []
            if isinstance(item, ast.Import):
                targets = [alias.name for alias in item.names]
            elif isinstance(item, ast.ImportFrom):
                base = package.split(".")[:len(package.split(".")) - item.level + 1] if item.level else []
                module = ".".join(base + ([item.module] if item.module else []))
                targets = [module] + [f"{module}.{alias.name}" for alias in item.names]
            for target in targets:
                if target in modules and modules[target] != path:
                    imports.add((path, modules[target], item.lineno))
    return dict(commit=commit, root=root, counts=dict(sorted(counts.items())),
                imports=[dict(source=f"file:{source}", target=f"file:{target}", line=line)
                         for source, target, line in sorted(imports)])


def main():
    """Write deterministic browser data without embedding source bodies."""
    data = build()
    (ROOT / OUTPUT).write_text("window.CODE_HIERARCHY = " + json.dumps(data, separators=(",", ":")) + ";\n",
                               encoding="utf-8")
    print(f"Wrote {OUTPUT}: {data['counts']['file']} files, {data['counts'].get('function', 0)} functions")


if __name__ == "__main__":
    main()
