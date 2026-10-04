"""Protect the caller/callee boundary of the public source call map."""

import ast
import unittest

from build_call_graph import CallGraph, ROOT, build


class CallGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = build()
        cls.edges = cls.data["edges"]
        cls.nodes = {node["id"]: node for node in cls.data["nodes"]}

    def edges_between(self, source, target):
        return [edge for edge in self.edges if edge["source"] == source and edge["target"] == target]

    def test_entry_and_normal_run_edges_have_real_call_sites(self):
        pairs = [
            ("monitor:module", "stock_monitor.cli.main"),
            ("stock_monitor.cli.main", "stock_monitor.cli.run"),
            ("stock_monitor.cli.run", "stock_monitor.cli.run_robinhood"),
            ("stock_monitor.cli.run_robinhood", "stock_monitor.cli.poll"),
            ("stock_monitor.cli.poll", "stock_monitor.robinhood_source.RobinhoodSource.fetch"),
            ("stock_monitor.cli.poll", "stock_monitor.cli.process_snapshot"),
            ("stock_monitor.cli.process_snapshot", "stock_monitor.rules.evaluate"),
            ("stock_monitor.cli.process_snapshot", "stock_monitor.runtime.AlertState.process"),
            ("stock_monitor.runtime.notify", "stock_monitor.notifications.send_ntfy"),
        ]
        for source, target in pairs:
            with self.subTest(source=source, target=target):
                edges = self.edges_between(source, target)
                self.assertTrue(edges)
                for edge in edges:
                    tree = ast.parse((ROOT / edge["path"]).read_text())
                    self.assertTrue(any(isinstance(node, ast.Call) and node.lineno == edge["line"]
                                        and ast.unparse(node) == edge["expression"] for node in ast.walk(tree)))
                    self.assertTrue(edge["sourceUrl"].endswith(f"{edge['path']}#L{edge['line']}"))

    def test_dataflow_is_not_mislabeled_as_calls(self):
        self.assertFalse(self.edges_between("stock_monitor.robinhood_source.RobinhoodSource.fetch", "stock_monitor.rules.evaluate"))
        self.assertFalse(self.edges_between("stock_monitor.rules.evaluate", "stock_monitor.runtime.notify"))
        self.assertFalse(self.edges_between("stock_monitor.cli.process_snapshot", "stock_monitor.runtime.notify"))
        # The real invocation of the callback happens in the state machine.
        edge, = self.edges_between("stock_monitor.runtime.AlertState.process", "stock_monitor.runtime.notify")
        self.assertEqual(edge["kind"], "callback")
        self.assertEqual(edge["expression"], "deliver()")
        self.assertEqual(edge["binding"]["path"], "src/stock_monitor/cli.py")
        self.assertIn("lambda: notify(", edge["binding"]["expression"])

    def test_injected_transport_and_external_boundaries_are_explicit(self):
        for caller in ("fetch", "_historicals"):
            edge, = self.edges_between("stock_monitor.robinhood_source.RobinhoodSource." + caller,
                                       "stock_monitor.robinhood_client.RobinhoodClient.call_tool")
            self.assertEqual(edge["kind"], "injected")
            self.assertEqual(edge["binding"]["expression"], "source.call_tool = client.call_tool")
        edge, = self.edges_between("stock_monitor.robinhood_client.RobinhoodClient.call_tool", "mcp.Client.call_tool")
        self.assertEqual(edge["kind"], "await")
        edge, = self.edges_between("stock_monitor.notifications.send_ntfy", "urllib.request.OpenerDirector.open")
        self.assertIn("build_opener(_NoRedirect()).open(", edge["expression"])
        self.assertTrue(self.nodes[edge["target"]]["external"])

    def test_nested_bodies_are_not_attributed_to_enclosing_callers(self):
        nested = "stock_monitor.runtime.parse_snapshot.volume"
        self.assertTrue(self.edges_between("stock_monitor.runtime.parse_snapshot", nested))
        self.assertTrue(self.edges_between(nested, "stock_monitor.rules.VolumeObservation"))
        self.assertFalse(self.edges_between("stock_monitor.runtime.parse_snapshot", "stock_monitor.rules.VolumeObservation"))
        data = CallGraph({"monitor.py": """def target():
    pass

def outer():
    def inner():
        target()
    callback = lambda: target()
    consume(callback)
    inner()
"""}).build()
        pairs = {(edge["source"], edge["target"]) for edge in data["edges"]}
        self.assertIn(("monitor.outer.inner", "monitor.target"), pairs)
        self.assertIn(("monitor.outer", "monitor.outer.inner"), pairs)
        self.assertNotIn(("monitor.outer", "monitor.target"), pairs)
        self.assertNotIn(("monitor:module", "monitor.target"), pairs)

    def test_passing_a_function_is_not_calling_it_and_parameters_shadow_imports(self):
        sources = {"monitor.py": """from stock_monitor.rules import evaluate

def handler(evaluate):
    evaluate()

def setup():
    register(evaluate)
""", "src/stock_monitor/rules.py": "def evaluate():\n    pass\n"}
        data = CallGraph(sources).build()
        self.assertFalse(data["edges"])
        nodes = {node["id"]: node for node in data["nodes"]}
        self.assertEqual(nodes["monitor.handler"]["unresolvedCount"], 1)
        self.assertEqual(nodes["monitor.setup"]["unresolvedCount"], 1)

    def test_reassigned_instances_are_not_guessed(self):
        data = CallGraph({"monitor.py": """class Example:
    def execute(self):
        pass

def run():
    value = Example()
    value = unknown()
    value.execute()
"""}).build()
        self.assertFalse(any(edge["target"] == "monitor.Example.execute" for edge in data["edges"]))

    def test_explicit_dynamic_bindings_must_still_exist_in_source(self):
        paths = ["monitor.py", *sorted(path.relative_to(ROOT).as_posix()
                                      for path in (ROOT / "src/stock_monitor").glob("*.py"))]
        sources = {path: (ROOT / path).read_text() for path in paths}
        sources["src/stock_monitor/cli.py"] = sources["src/stock_monitor/cli.py"].replace(
            "source.call_tool = client.call_tool", "source.call_tool = other_callable").replace(
            "lambda: notify(config[", "lambda: other_notify(config[")
        sources["src/stock_monitor/robinhood_client.py"] = sources["src/stock_monitor/robinhood_client.py"].replace(
            "enter_async_context(Client(", "enter_async_context(OtherClient(")
        data = CallGraph(sources).build()
        self.assertFalse(any(edge["kind"] in {"injected", "callback"} for edge in data["edges"]))
        self.assertFalse(any(edge["target"] == "mcp.Client.call_tool" for edge in data["edges"]))

    def test_context_and_constructor_dispatch_are_labeled_not_fabricated_direct_calls(self):
        for method in ("__aenter__", "__aexit__"):
            edge, = self.edges_between("stock_monitor.cli.run_robinhood", "stock_monitor.robinhood_client.RobinhoodClient." + method)
            self.assertEqual(edge["kind"], "context")
            self.assertTrue(edge["expression"].startswith("RobinhoodClient("))
        edge, = self.edges_between("stock_monitor.cli.run_robinhood", "stock_monitor.robinhood_source.RobinhoodSource.__init__")
        self.assertEqual(edge["kind"], "constructor")
        self.assertTrue(edge["expression"].startswith("RobinhoodSource("))

    def test_deterministic_source_only_graph_has_valid_unique_nodes_and_edges(self):
        self.assertEqual(self.data, build())
        self.assertEqual(len(self.nodes), len(self.data["nodes"]))
        self.assertEqual(len({edge["id"] for edge in self.edges}), len(self.edges))
        for edge in self.edges:
            self.assertIn(edge["source"], self.nodes)
            self.assertIn(edge["target"], self.nodes)
            self.assertTrue(edge["path"] == "monitor.py" or edge["path"].startswith("src/stock_monitor/"))
        self.assertIn("Partial static", self.data["scopeNote"])
        self.assertGreater(sum(sum(node["omittedCalls"].values()) for node in self.nodes.values()), 0)

    def test_state_save_reasons_preserve_separate_callsites(self):
        edges = self.edges_between("stock_monitor.runtime.AlertState.process", "stock_monitor.runtime.AlertState.save")
        self.assertEqual(len(edges), 3)
        self.assertEqual(len({edge["line"] for edge in edges}), 3)
        notes = " ".join(edge["note"] for edge in edges)
        for phrase in ("persistence failure", "rearming", "after delivery"):
            self.assertIn(phrase, notes)


if __name__ == "__main__":
    unittest.main()
