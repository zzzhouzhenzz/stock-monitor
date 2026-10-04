"""Regression checks for the public repository hierarchy, separate from runtime tests."""

import ast
from collections import Counter
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build_hierarchy as hierarchy


def walk(node):
    yield node
    for child in node["children"]:
        yield from walk(child)


def child(node, name):
    return next(item for item in node["children"] if item["name"] == name)


class HierarchyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = hierarchy.build()
        cls.files = {node["path"]: node for node in walk(cls.data["root"]) if node["kind"] == "file"}

    def test_imported_entry_point_is_not_a_definition_and_async_runner_stays_in_cli(self):
        self.assertNotIn("main", [node["name"] for node in self.files["monitor.py"]["children"]])
        runner = child(self.files["src/stock_monitor/cli.py"], "run_robinhood")
        self.assertEqual(runner["kind"], "function")
        self.assertTrue(runner["async"])
        self.assertTrue(runner["signature"].startswith("async def run_robinhood("))

    def test_methods_and_nested_functions_keep_their_actual_parents(self):
        runtime = self.files["src/stock_monitor/runtime.py"]
        state = child(runtime, "AlertState")
        self.assertEqual(state["kind"], "class")
        self.assertEqual(child(state, "process")["kind"], "function")
        parser = child(runtime, "parse_snapshot")
        self.assertEqual(child(parser, "volume")["kind"], "function")
        self.assertTrue({"process", "volume"}.isdisjoint(node["name"] for node in runtime["children"]))

    def test_private_paths_are_rejected_before_contents_are_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public = ["public.py", "changed.py", "new.py"]
            private = [".state/private.py", "config.local.py", "credentials.py", ".env.py", ".venv/private.py"]
            for name in [*public, *private]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("def visible():\n    pass\n" if name in public else "PRIVATE - DO NOT PARSE")
            (root / "linked.py").symlink_to(root / ".state/private.py")
            responses = {("ls-files", "-z"): "\0".join([*public, "linked.py", *private]),
                         ("rev-parse", "HEAD"): "0" * 40,
                         ("ls-tree", "-r", "--name-only", "-z", "HEAD"): "public.py\0changed.py",
                         ("diff", "HEAD", "--name-only", "-z"): "changed.py\0new.py",
                         ("branch", "--show-current"): "codex/hierarchy",
                         ("remote", "get-url", "origin"): "https://github.com/example/stock-monitor.git"}
            original_read = Path.read_text
            reads = []

            def guarded_read(path, *args, **kwargs):
                self.assertIn(path, [root / name for name in public], "A private file reached the read step")
                reads.append(path)
                return original_read(path, *args, **kwargs)

            with patch.object(hierarchy, "ROOT", root), patch.object(hierarchy, "PUBLIC_DOCS", set()), \
                    patch.object(hierarchy, "git", side_effect=lambda *args: responses[args]), \
                    patch.object(Path, "read_text", guarded_read):
                result = hierarchy.build()
                self.assertEqual(reads, [root / name for name in sorted(public)])
                responses[("branch", "--show-current")] = ""
                detached = hierarchy.build()
                responses[("branch", "--show-current")] = "codex/hierarchy"
                responses[("remote", "get-url", "origin")] = "https://example.invalid/stock-monitor.git"
                unfamiliar = hierarchy.build()
            files = {node["path"]: node for node in walk(result["root"]) if node["kind"] == "file"}
            self.assertEqual(set(files), set(public))
            self.assertIn("/blob/" + "0" * 40 + "/public.py", files["public.py"]["sourceUrl"])
            self.assertNotIn("sourceBranchUrl", files["public.py"])
            for name in ("changed.py", "new.py"):
                self.assertNotIn("sourceUrl", files[name])
                self.assertEqual(files[name]["sourceBranchUrl"],
                                 "https://github.com/example/stock-monitor/blob/codex%2Fhierarchy/" + name)
                self.assertEqual(files[name]["sourceHref"], "../" + name)
            self.assertFalse(any("sourceBranchUrl" in node for node in walk(detached["root"])))
            self.assertFalse(any("sourceBranchUrl" in node or "sourceUrl" in node for node in walk(unfamiliar["root"])))

    def test_every_symbol_range_matches_the_real_ast(self):
        for path, file in self.files.items():
            if not path.endswith(".py"):
                continue
            with self.subTest(path=path):
                text = (hierarchy.ROOT / path).read_text(encoding="utf-8")
                tree = ast.parse(text)
                expected = Counter(("class" if isinstance(node, ast.ClassDef) else "function",
                                    node.name, node.lineno, node.end_lineno)
                                   for node in ast.walk(tree)
                                   if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)))
                actual = Counter((node["kind"], node["name"], node["line"], node["endLine"])
                                 for node in walk(file) if node["kind"] in {"class", "function"})
                self.assertEqual(actual, expected)
                annotations = {(node.target.id, node.lineno, node.end_lineno) for node in ast.walk(tree)
                               if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)}
                for node in walk(file):
                    if node["kind"] == "field":
                        self.assertIn((node["name"], node["line"], node["endLine"]), annotations)
                self.assertEqual(file["endLine"], len(text.splitlines()))

    def test_generation_is_deterministic_and_ids_and_import_targets_are_valid(self):
        self.assertEqual(self.data, hierarchy.build())
        nodes = list(walk(self.data["root"]))
        ids = {node["id"] for node in nodes}
        self.assertEqual(len(ids), len(nodes))
        file_ids = {node["id"] for node in self.files.values()}
        for edge in self.data["imports"]:
            self.assertIn(edge["source"], file_ids)
            self.assertIn(edge["target"], file_ids)


if __name__ == "__main__":
    unittest.main()
