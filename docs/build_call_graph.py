"""Build a conservative, source-only call map for the monitor's production code.

This is deliberately not whole-program type inference. Local names and obvious
constructed instances are resolved; a few dependency-injection bindings are
verified in the source. Other calls are counted, not guessed. No code is imported.
"""

import ast
import builtins
from collections import Counter
import json
from pathlib import Path

from build_hierarchy import ROOT, git, github_base, module_name, public_path, summary


OUTPUT = "docs/callgraph-data.js"
PREFIX = "stock_monitor."
EXTERNALS = {
    "asyncio.sleep": "Yield to the event loop until the configured delay expires.",
    "mcp.Client.call_tool": "MCP SDK boundary; sends a tool request over the configured Streamable HTTP transport.",
    "urllib.request.OpenerDirector.open": "HTTP boundary; sends the ntfy POST and returns its response. Acceptance is not phone delivery confirmation.",
}


def qualified(module, name):
    return module + "." + name


def own_nodes(body):
    """Walk executable syntax without assigning nested function bodies to a parent."""
    for item in body:
        yield item
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield from own_nodes(ast.iter_child_nodes(item))


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        value = dotted(node.value)
        return value + "." + node.attr if value else ""
    return ""


class CallGraph:
    def __init__(self, sources, commit="", base="", unchanged=None):
        self.sources = sources
        self.commit, self.base = commit, base
        self.unchanged = set(sources) if unchanged is None else unchanged
        self.nodes, self.scopes, self.edges = {}, {}, []
        self.modules = {module_name(path): path for path in sources}
        for path, text in sources.items():
            module = module_name(path)
            tree = ast.parse(text, filename=path)
            self._register(module + ":module", module, path, tree, None, None)
            self._definitions(tree.body, module, path, "", module + ":module", None)
        self.bindings = self._verified_bindings()

    def link(self, path, line):
        return f"{self.base}{path}#L{line}" if self.base and path in self.unchanged else ""

    def _register(self, identifier, module, path, tree, parent, owner):
        name = getattr(tree, "name", "module body")
        kind = "module" if isinstance(tree, ast.Module) else "class" if isinstance(tree, ast.ClassDef) else "method" if owner else "function"
        signature = ""
        if isinstance(tree, (ast.FunctionDef, ast.AsyncFunctionDef)):
            signature = ("async " if isinstance(tree, ast.AsyncFunctionDef) else "") + f"def {name}({ast.unparse(tree.args)})"
        elif isinstance(tree, ast.ClassDef):
            signature = "class " + name
        line = getattr(tree, "lineno", 1)
        label = identifier.removeprefix(module + ".") if kind != "module" else Path(path).name + " (entry)" if path == "monitor.py" else Path(path).name + " (module)"
        self.nodes[identifier] = dict(id=identifier, label=label, name=name, module=module, path=path,
                                     line=line, endLine=getattr(tree, "end_lineno", len(self.sources[path].splitlines())),
                                     kind=kind, signature=signature, doc=summary(tree), sourceUrl=self.link(path, line),
                                     unresolvedCount=0, omittedCalls={}, parent=parent, asyncFunction=isinstance(tree, ast.AsyncFunctionDef))
        self.scopes[identifier] = dict(tree=tree, module=module, path=path, parent=parent, owner=owner)

    def _definitions(self, body, module, path, prefix, parent, owner):
        for item in body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                local = prefix + "." + item.name if prefix else item.name
                identifier = qualified(module, local)
                self._register(identifier, module, path, item, parent, owner)
                self._definitions(item.body, module, path, local, identifier,
                                  identifier if isinstance(item, ast.ClassDef) else None)
            else:
                self._definitions(ast.iter_child_nodes(item), module, path, prefix, parent, owner)

    def _binding(self, scope, predicate):
        if scope not in self.scopes:
            return None
        info = self.scopes[scope]
        found = next((node for node in own_nodes(info["tree"].body) if predicate(node)), None)
        if found is None:
            return None
        return dict(path=info["path"], line=found.lineno, expression=ast.unparse(found),
                    sourceUrl=self.link(info["path"], found.lineno))

    def _verified_bindings(self):
        runner = PREFIX + "cli.run_robinhood"
        constructor = self._binding(runner, lambda n: isinstance(n, ast.Assign)
                                    and any(dotted(t) == "source" for t in n.targets)
                                    and isinstance(n.value, ast.Call) and dotted(n.value.func) == "RobinhoodSource")
        source_argument = self._binding(runner, lambda n: isinstance(n, ast.Call)
                                        and dotted(n.func) == "poll" and len(n.args) == 6
                                        and dotted(n.args[5]) == "source")
        constructor = constructor if source_argument else None
        injection = self._binding(runner, lambda n: isinstance(n, ast.Assign)
                                  and any(dotted(t) == "source.call_tool" for t in n.targets)
                                  and dotted(n.value) == "client.call_tool")
        client = self._binding(runner, lambda n: isinstance(n, ast.AsyncWith)
                              and any(isinstance(i.context_expr, ast.Call) and dotted(i.context_expr.func) == "RobinhoodClient"
                                      and dotted(i.optional_vars) == "client" for i in n.items))
        state = self._binding(PREFIX + "cli.run", lambda n: isinstance(n, ast.Assign)
                             and any(dotted(t) == "state" for t in n.targets)
                             and isinstance(n.value, ast.IfExp) and isinstance(n.value.orelse, ast.Call)
                             and dotted(n.value.orelse.func) == "AlertState")
        callback = self._binding(PREFIX + "cli.process_snapshot", lambda n: isinstance(n, ast.Call)
                                and dotted(n.func) == "state.process" and len(n.args) == 6
                                and isinstance(n.args[5], ast.Lambda) and isinstance(n.args[5].body, ast.Call)
                                and dotted(n.args[5].body.func) == "notify")
        transport = self._binding(PREFIX + "robinhood_client.RobinhoodClient.__aenter__",
                                  lambda n: isinstance(n, ast.Assign)
                                  and any(dotted(t) == "self._client" for t in n.targets)
                                  and isinstance(n.value, ast.Await) and isinstance(n.value.value, ast.Call)
                                  and dotted(n.value.value.func) == "self._stack.enter_async_context"
                                  and len(n.value.value.args) == 1 and isinstance(n.value.value.args[0], ast.Call)
                                  and dotted(n.value.value.args[0].func) == "Client")
        return dict(source=constructor, injection=injection if constructor and client else None,
                    state=state, callback=callback if state else None, transport=transport)

    def _environment(self, identifier):
        info = self.scopes[identifier]
        chain, cursor = [], identifier
        while cursor:
            chain.append(cursor)
            cursor = self.scopes[cursor]["parent"]
        environment = {}
        for scope in reversed(chain):
            current = self.scopes[scope]
            module = current["module"]
            for child, child_info in self.scopes.items():
                if child_info["parent"] == scope:
                    environment[self.nodes[child]["name"]] = child
            for node in own_nodes(current["tree"].body):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        environment[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
                elif isinstance(node, ast.ImportFrom):
                    package = module if current["path"].endswith("/__init__.py") else module.rpartition(".")[0]
                    parts = package.split(".")[:len(package.split(".")) - node.level + 1] if node.level else []
                    imported = ".".join(parts + ([node.module] if node.module else []))
                    for alias in node.names:
                        environment[alias.asname or alias.name] = imported + "." + alias.name
        tree = info["tree"]
        # Parameters shadow enclosing names. Unknown arguments stay unresolved.
        if isinstance(tree, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = tree.args
            for argument in [*args.posonlyargs, *args.args, *args.kwonlyargs, *([args.vararg] if args.vararg else []), *([args.kwarg] if args.kwarg else [])]:
                environment.pop(argument.arg, None)
        return environment

    def _target(self, expression, env, instances):
        if isinstance(expression, ast.Name):
            return env.get(expression.id, "")
        if isinstance(expression, ast.Attribute):
            base = dotted(expression.value)
            if base in instances:
                return instances[base] + "." + expression.attr
            root = self._target(expression.value, env, instances)
            return root + "." + expression.attr if root else ""
        if isinstance(expression, ast.Call):
            value = self._target(expression.func, env, instances)
            return value if value in self.nodes and self.nodes[value]["kind"] == "class" else ""
        return ""

    def _add_edge(self, source, target, call, kind, note="", binding=None):
        info = self.scopes[source]
        if target in EXTERNALS and target not in self.nodes:
            self.nodes[target] = dict(id=target, label=target, name=target.rsplit(".", 1)[-1], module=target.rsplit(".", 1)[0],
                                      path="", line=None, endLine=None, kind="external", signature=target + "(…)",
                                      doc=EXTERNALS[target], sourceUrl="", external=True, unresolvedCount=0, omittedCalls={})
        edge = dict(id=f"{source}:{call.lineno}:{getattr(call, 'col_offset', 0)}:{target}:{kind}", source=source, target=target,
                    line=call.lineno, path=info["path"], expression=ast.unparse(call), kind=kind,
                    note=note, sourceUrl=self.link(info["path"], call.lineno))
        if binding:
            edge["binding"] = binding
        self.edges.append(edge)

    def _note(self, source, target, call):
        pair = (source.removeprefix(PREFIX), target.removeprefix(PREFIX))
        if pair == ("cli.run_robinhood", "robinhood_source.RobinhoodSource.next_open"):
            return "Market closed: compute the next regular-session opening before any broker connection."
        if pair == ("cli.run_robinhood", "asyncio.sleep"):
            return "Market closed: sleep until next open." if "opens" in ast.unparse(call) else "Transport retry: back off, bounded by the session close."
        notes = {
            ("cli.poll", "robinhood_source.RobinhoodSource.fetch"): "Only after the calendar says the regular session is open.",
            ("cli.poll", "cli.process_snapshot"): "Process the fetched or file-backed snapshot; recheck the clock after I/O.",
            ("cli.poll", "asyncio.sleep"): "Wait the configured polling delay (120 seconds in the example), bounded by session close.",
            ("cli.process_snapshot", "rules.evaluate"): "Evaluate each configured price rule against the same normalized quote and volume.",
            ("cli.process_snapshot", "runtime.AlertState.process"): "Eligible observation and not dry-run: update the alert episode or invoke delivery.",
            ("runtime.notify", "notifications.send_ntfy"): "Only when notification channel is ntfy.",
            ("notifications.send_ntfy", "urllib.request.OpenerDirector.open"): "Send a single HTTPS POST. A server acknowledgment does not prove phone delivery.",
        }
        if pair == ("runtime.AlertState.process", "runtime.AlertState.save"):
            lines = self.sources[self.scopes[source]["path"]].splitlines()
            context = " ".join(lines[max(0, call.lineno - 4):call.lineno])
            if "if self._dirty" in context:
                return "Retry a prior persistence failure before another state transition."
            if "active=False" in context:
                return "Persist rearming after a valid observation no longer matches."
            return "Persist the active episode after delivery returns successfully."
        return notes.get(pair, "")

    def _scan(self, identifier):
        info = self.scopes[identifier]
        env = self._environment(identifier)
        items = list(own_nodes(info["tree"].body))
        parents = {child: node for node in items for child in ast.iter_child_nodes(node)}
        assigned = Counter(node.id for node in items if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store))
        for name in assigned:
            env.pop(name, None)  # Do not mistake a local reassignment for an imported callable.
        instances = {"self": info["owner"]} if info["owner"] else {}
        if identifier == PREFIX + "cli.poll" and self.bindings["source"]:
            instances["source"] = PREFIX + "robinhood_source.RobinhoodSource"
        if identifier == PREFIX + "cli.process_snapshot" and self.bindings["state"]:
            instances["state"] = PREFIX + "runtime.AlertState"
        # Infer only simple local constructor assignments and async-with aliases.
        for node in items:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                target = self._target(node.value.func, env, instances)
                if target in self.nodes and self.nodes[target]["kind"] == "class":
                    for name in node.targets:
                        if isinstance(name, ast.Name) and assigned[name.id] == 1:
                            instances[name.id] = target
            if isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if isinstance(item.context_expr, ast.Call) and isinstance(item.optional_vars, ast.Name):
                        target = self._target(item.context_expr.func, env, instances)
                        if target in self.nodes and self.nodes[target]["kind"] == "class":
                            instances[item.optional_vars.id] = target
                            if isinstance(node, ast.AsyncWith):
                                for method in ("__aenter__", "__aexit__"):
                                    if target + "." + method in self.nodes:
                                        self._add_edge(identifier, target + "." + method, item.context_expr, "context",
                                                       "Implicit async-context " + ("entry." if method == "__aenter__" else "exit, including exception cleanup."))
        omitted = Counter()
        for call in (node for node in items if isinstance(node, ast.Call)):
            target = self._target(call.func, env, instances)
            kind = "await" if isinstance(parents.get(call), ast.Await) else "direct"
            binding = None
            expression = dotted(call.func)
            if info["owner"] == PREFIX + "robinhood_source.RobinhoodSource" and expression == "self.call_tool" and self.bindings["injection"]:
                target, kind, binding = PREFIX + "robinhood_client.RobinhoodClient.call_tool", "injected", self.bindings["injection"]
            elif identifier == PREFIX + "runtime.AlertState.process" and expression == "deliver" and self.bindings["callback"]:
                target, kind, binding = PREFIX + "runtime.notify", "callback", self.bindings["callback"]
            elif identifier == PREFIX + "robinhood_client.RobinhoodClient.call_tool" and expression == "self._client.call_tool" and self.bindings["transport"]:
                target, binding = "mcp.Client.call_tool", self.bindings["transport"]
            elif identifier == PREFIX + "notifications.send_ntfy" and isinstance(call.func, ast.Attribute) and call.func.attr == "open" and isinstance(call.func.value, ast.Call) and self._target(call.func.value.func, env, instances) == "urllib.request.build_opener":
                target = "urllib.request.OpenerDirector.open"
            if target in self.nodes or target in EXTERNALS:
                note = self._note(identifier, target, call)
                if target in self.nodes and self.nodes[target]["kind"] == "class":
                    kind = "constructor"
                    if target + ".__init__" in self.nodes:
                        target += ".__init__"
                        note = "Construct the object; Python dispatches to this __init__."
                    else:
                        note = "Construct the class. Generated dataclass or inherited initialization is not expanded."
                if kind == "injected":
                    note = "Await the injected market-data callable; run_robinhood assigns client.call_tool."
                elif kind == "callback":
                    note = "Invoke deliver only for a new matching episode outside cooldown. Its lambda is supplied by process_snapshot; this is not a direct notify call."
                elif expression.startswith("source.") and identifier == PREFIX + "cli.poll":
                    binding = self.bindings["source"]
                elif expression == "state.process" and identifier == PREFIX + "cli.process_snapshot":
                    binding = self.bindings["state"]
                self._add_edge(identifier, target, call, kind, note, binding)
            elif isinstance(call.func, ast.Name) and call.func.id in vars(builtins):
                omitted["builtins"] += 1
            elif target and target.split(".")[0] not in {"stock_monitor", "monitor"}:
                omitted["library"] += 1
            else:
                omitted["unresolved"] += 1
        self.nodes[identifier]["omittedCalls"] = dict(omitted)
        self.nodes[identifier]["unresolvedCount"] = omitted["unresolved"]

    def build(self):
        for identifier in self.scopes:
            self._scan(identifier)
        return dict(commit=self.commit, nodes=sorted(self.nodes.values(), key=lambda node: node["id"]),
                    edges=sorted(self.edges, key=lambda edge: (edge["path"], edge["line"], edge["id"])),
                    defaultNode=PREFIX + "cli.poll", entryNode="monitor:module",
                    scopeNote="Partial static call map of monitor.py and src/stock_monitor only. Resolves local calls, self methods, obvious local instances, and the explicitly verified source/state/tool/delivery bindings. Calls in nested functions stay with that function. Callbacks and context-manager dispatch are labeled. Other library/builtin calls and unresolved dynamic calls are counted per caller; decorators, generated dataclass methods, external SDK callbacks, and complete runtime order are not expanded.")


def build():
    paths = ["monitor.py", *sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / "src/stock_monitor").glob("*.py"))]
    sources = {path: (ROOT / path).read_text(encoding="utf-8") for path in paths if public_path(path)}
    commit = git("rev-parse", "HEAD")
    changed = set(git("diff", "HEAD", "--name-only", "-z").split("\0"))
    committed = set(git("ls-tree", "-r", "--name-only", "-z", "HEAD").split("\0"))
    return CallGraph(sources, commit, github_base(commit), committed - changed).build()


def main():
    data = build()
    (ROOT / OUTPUT).write_text("window.CALL_GRAPH = " + json.dumps(data, separators=(",", ":")) + ";\n", encoding="utf-8")
    print(f"Wrote {OUTPUT}: {len(data['nodes'])} nodes; {len(data['edges'])} call sites")


if __name__ == "__main__":
    main()
