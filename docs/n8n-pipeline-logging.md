# n8n pipeline logging

`bin/n8n-pipeline-log` reads n8n's durable event stream and renders the complete
node path for a root execution. It includes synchronous `Execute Workflow`
children, indented beneath the caller.

Follow a running execution:

```bash
bin/n8n-pipeline-log 222435 --follow
```

With no arguments, the logger selects the latest root execution (ignoring
integrated sub-workflow executions) and follows it until completion:

```bash
bin/n8n-pipeline-log
```

Print the trace collected so far:

```bash
bin/n8n-pipeline-log 222435
```

Emit JSONL for filtering or ingestion:

```bash
bin/n8n-pipeline-log 222435 --follow --json
```

The default source is `~/.n8n/n8nEventLog*.log`. Override it with
`N8N_LOG_DIR=/path/to/n8n-data` or `--log-dir`.

This logger deliberately uses the event stream instead of the executions API:
n8n does not persist `runData` for a running long-duration node, while node
start/finish events are written immediately. The root workflow's terminal event
ends `--follow`; Ctrl-C exits an intentionally interrupted follow with status
130.
