import importlib.machinery
import importlib.util
import json
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "bin" / "n8n-pipeline-log"
loader = importlib.machinery.SourceFileLoader("n8n_pipeline_log", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = module
loader.exec_module(module)


def event(name, execution_id, node=None, node_type=None, workflow="Parent"):
    payload = {
        "executionId": str(execution_id),
        "workflowId": "wf",
        "workflowName": workflow,
    }
    if node:
        payload.update(nodeName=node, nodeType=node_type)
    return json.dumps(
        {"ts": f"2026-07-28T12:00:0{execution_id}.000-04:00", "eventName": name, "payload": payload}
    )


class PipelineTraceTests(unittest.TestCase):
    def test_correlates_synchronous_child_workflow(self):
        lines = [
            event("n8n.workflow.started", 1),
            event("n8n.node.started", 1, "Parse", "n8n-nodes-base.executeWorkflow"),
            event("n8n.workflow.started", 2, workflow="Child"),
            event("n8n.node.started", 2, "Validate", "n8n-nodes-base.code", "Child"),
            event("n8n.node.finished", 2, "Validate", "n8n-nodes-base.code", "Child"),
            event("n8n.workflow.success", 2, workflow="Child"),
            event("n8n.node.finished", 1, "Parse", "n8n-nodes-base.executeWorkflow"),
            event("n8n.workflow.success", 1),
        ]
        trace = module.PipelineTrace("1")
        accepted = [trace.accept(module.parse_line(line)) for line in lines]
        self.assertEqual([item[1] for item in accepted if item], [0, 0, 1, 1, 1, 1, 0, 0])
        self.assertTrue(trace.finished)

    def test_ignores_unrelated_execution(self):
        trace = module.PipelineTrace("1")
        self.assertIsNone(trace.accept(module.parse_line(event("n8n.workflow.started", 99))))
        self.assertIsNotNone(trace.accept(module.parse_line(event("n8n.workflow.started", 1))))
        self.assertIsNone(trace.accept(module.parse_line(event("n8n.node.started", 99, "Other", "x"))))

    def test_malformed_line_is_ignored(self):
        self.assertIsNone(module.parse_line("not-json"))

    def test_latest_root_excludes_integrated_child(self):
        root_1 = module.parse_line(event("n8n.workflow.started", 1))
        child = module.parse_line(event("n8n.workflow.started", 2, workflow="Child"))
        root_3 = module.parse_line(event("n8n.workflow.started", 3))
        child = module.TraceEvent(**{**child.__dict__, "execution_mode": "integrated"})
        root_1 = module.TraceEvent(**{**root_1.__dict__, "execution_mode": "trigger"})
        root_3 = module.TraceEvent(**{**root_3.__dict__, "execution_mode": "manual"})

        latest = module.latest_root_execution([root_1, child, root_3])

        self.assertEqual(latest.execution_id, "3")

    def test_no_argument_defaults_to_latest_execution(self):
        args = module.build_parser().parse_args([])
        self.assertIsNone(args.execution_id)
        self.assertFalse(args.follow)


if __name__ == "__main__":
    unittest.main()
