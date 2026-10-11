"""Import-direction checks that keep the package boundaries honest.

Reads source with ``ast`` (nothing is imported or executed) and asserts:
the question pipeline (conversation, planning, analytics) never imports
storage, ingestion or the server; analytics depends only on the fact
contract; storage never imports the pipeline or the model client; the HTTP
input validators depend on nothing in the package; and there is no runtime
import cycle. Imports under ``if TYPE_CHECKING:`` are annotations only and
are ignored.
"""

import ast
import unittest
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "airport_intel"
PACKAGE = "airport_intel"

PIPELINE = {"conversation", "planning", "analytics", "facts"}
WIRING_AND_IO = {"storage", "ingestion", "server"}


def _module_name(path: Path) -> str:
    parts = path.relative_to(PACKAGE_DIR).with_suffix("").parts
    return ".".join(part for part in parts if part != "__init__")


def _is_type_checking_block(node: ast.AST) -> bool:
    return isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"


def _runtime_imports(path: Path) -> set[str]:
    """Absolute names imported at runtime, relative imports resolved to ``airport_intel.*``."""
    module = _module_name(path)
    package_parts = [PACKAGE, *module.split(".")] if module else [PACKAGE]
    if path.name != "__init__.py":
        package_parts = package_parts[:-1]
    found: set[str] = set()

    def visit(node: ast.AST) -> None:
        if _is_type_checking_block(node):
            return
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package_parts[: len(package_parts) - (node.level - 1)]
                prefix = ".".join(base + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            if node.module is not None:
                found.add(prefix)
            found.update(f"{prefix}.{alias.name}" for alias in node.names)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(ast.parse(path.read_text(encoding="utf-8")))
    return found


def _package_graph() -> dict[str, set[str]]:
    """Internal module -> internal modules it imports at runtime."""
    modules = {_module_name(path): path for path in PACKAGE_DIR.rglob("*.py")}
    graph = {}
    for name, path in modules.items():
        targets = set()
        for imported in _runtime_imports(path):
            if not imported.startswith(PACKAGE + "."):
                continue
            dotted = imported[len(PACKAGE) + 1:]
            while dotted and dotted not in modules:
                dotted = dotted.rpartition(".")[0]
            if dotted and dotted != name:
                targets.add(dotted)
        graph[name] = targets
    return graph


def _top(name: str) -> str:
    return name.split(".")[0]


class ArchitectureBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = _package_graph()
        cls.external = {
            _module_name(path): {name.split(".")[0] for name in _runtime_imports(path)}
            for path in PACKAGE_DIR.rglob("*.py")
        }

    def test_question_pipeline_never_imports_storage_ingestion_or_server(self):
        for module, targets in self.graph.items():
            if _top(module) not in PIPELINE:
                continue
            internal = {_top(target) for target in targets}
            with self.subTest(module=module):
                self.assertFalse(internal & WIRING_AND_IO, internal & WIRING_AND_IO)
                self.assertFalse(self.external[module] & {"sqlite3", "http"})

    def test_analytics_depends_only_on_the_fact_contract(self):
        for module, targets in self.graph.items():
            if _top(module) != "analytics":
                continue
            with self.subTest(module=module):
                self.assertTrue({_top(t) for t in targets} <= {"analytics", "facts"}, targets)

    def test_storage_never_imports_pipeline_model_or_http(self):
        # Two deliberate exceptions: source row-count floors and the persisted context type.
        allowed = {"ingestion.live_sources", "conversation.context"}
        for module, targets in self.graph.items():
            if _top(module) != "storage":
                continue
            with self.subTest(module=module):
                outside = {t for t in targets if _top(t) != "storage"}
                self.assertTrue(outside <= allowed, outside)

    def test_http_input_validation_is_package_independent(self):
        self.assertEqual(self.graph["server.request_validation"], set())

    def test_only_the_server_knows_the_composition_root(self):
        importers_of_runtime = {module for module, targets in self.graph.items() if "server.runtime" in targets}
        self.assertEqual(importers_of_runtime, {"server.app", "server.__main__"})

    def test_no_runtime_import_cycles(self):
        visiting, done = set(), set()

        def walk(node, trail):
            if node in done:
                return
            if node in visiting:
                self.fail("import cycle: " + " -> ".join(trail + [node]))
            visiting.add(node)
            for target in self.graph.get(node, ()):
                walk(target, trail + [node])
            visiting.discard(node)
            done.add(node)

        for module in self.graph:
            walk(module, [])


if __name__ == "__main__":
    unittest.main()
