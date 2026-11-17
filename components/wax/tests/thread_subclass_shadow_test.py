"""No wax Thread subclass may shadow a private attribute of threading.Thread.

Worker set `self._stop = threading.Event()`. On Python 3.12, Thread.join()
calls its own private `self._stop()`, so the Event shadowed it and shutdown
raised "'Event' object is not callable". 3.13 renamed the internals, so the
suite stayed green on the daemon's interpreter while being broken on 3.12.
This scans the source (any interpreter) for every Thread subclass, not just
Worker, and checks against the union of private names across 3.11-3.14.
"""
import ast
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src" / "wax"

# Private Thread internals across CPython 3.11-3.14; dir() adds the current one's.
THREAD_PRIVATE = {
    "_stop", "_started", "_is_stopped", "_tstate_lock", "_handle", "_target",
    "_args", "_kwargs", "_name", "_ident", "_native_id", "_daemonic",
    "_invoke_excepthook", "_bootstrap", "_bootstrap_inner", "_delete",
    "_reset_internal_locks", "_set_ident", "_set_native_id", "_set_tstate_lock",
    "_wait_for_tstate_lock", "_stderr", "_initialized", "_os_thread_handle",
    "_after_fork", "_set_os_name", "_context", "_ident",
} | {name for name in dir(threading.Thread) if name.startswith("_") and not name.startswith("__")}


def _is_thread_base(base: ast.expr) -> bool:
    return (isinstance(base, ast.Name) and base.id == "Thread") or (
        isinstance(base, ast.Attribute) and base.attr == "Thread")


def _self_attributes(target: ast.expr):
    if isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _self_attributes(element)
    elif isinstance(target, ast.Starred):
        yield from _self_attributes(target.value)
    elif (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
          and target.value.id == "self"):
        yield target.attr


def thread_subclass_assignments():
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef) or not any(map(_is_thread_base, cls.bases)):
                continue
            for node in ast.walk(cls):
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                    targets = [node.target]
                else:
                    continue
                for target in targets:
                    for attr in _self_attributes(target):
                        yield path.name, cls.name, attr, node.lineno


class ThreadSubclassShadowTest(unittest.TestCase):
    def test_scanner_assignment_targets(self):
        cases = (
            ("self._stop = threading.Event()", {"_stop"}),
            ("self._halt: object = threading.Event()", {"_halt"}),
            ("self._stop += 1", {"_stop"}),
            ("self._stop = self._halt = threading.Event()", {"_stop", "_halt"}),
            ("self._stop, self._halt = threading.Event(), threading.Event()",
             {"_stop", "_halt"}),
            ("[self._stop, [self._halt]] = [None, [None]]", {"_stop", "_halt"}),
            ("self.safe, (self._stop, *self._started) = (None, (None, None))",
             {"safe", "_stop", "_started"}),
            ("other._stop, local = (None, None)", set()),
            ("self.safe = None", {"safe"}),
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "shadow_fixture.py"
            with patch.object(__import__(__name__), "SRC", Path(directory)):
                for assignment, expected in cases:
                    with self.subTest(assignment=assignment):
                        source.write_text(
                            "import threading\nclass Probe(threading.Thread):\n"
                            "    def run(self):\n        " + assignment + "\n")
                        found = list(thread_subclass_assignments())
                        self.assertEqual({attr for _, _, attr, _ in found}, expected)
                        self.assertTrue(all(file == source.name and cls == "Probe" and line == 4
                                            for file, cls, _, line in found))
                        self.assertEqual(
                            {attr for _, _, attr, _ in found if attr in THREAD_PRIVATE},
                            expected & THREAD_PRIVATE)

    def test_the_scan_sees_the_worker_thread(self):
        self.assertIn("Worker", {cls for _, cls, _, _ in thread_subclass_assignments()})

    def test_no_thread_subclass_overwrites_a_thread_internal(self):
        clashes = [f"{file}:{line} {cls}.{attr}" for file, cls, attr, line in thread_subclass_assignments()
                   if attr in THREAD_PRIVATE]
        self.assertEqual(clashes, [], "rename these: threading.Thread uses the name itself")


if __name__ == "__main__":
    unittest.main()
