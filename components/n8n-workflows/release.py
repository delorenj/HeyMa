#!/usr/bin/python3
"""Release the "Transcription Notifications" n8n workflow (XHWRrrYCzu4ifdS7).

Dry run by default: GET the live workflow, build the candidate, print it (or write --out) and a
diff summary on stderr. --apply PUTs it, publishes it and verifies the result. A re-run against
the released workflow builds an identical candidate and changes nothing.
"""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

WORKFLOW_ID = "XHWRrrYCzu4ifdS7"
BASE = "https://n8n.delo.sh/api/v1"
API_REF = "op://DeLoSecrets/5pyy6ct5pi7eaz436g2qkn6pjq/mfiofpxjhlpubqwdzop3ty3eq4"
NTFY_REF = "op://DeLoSecrets/y2bcg7okwrpucnecdswlpcviti/credential"
# Live state reviewed 2026-10-08: the published version is still the 2026-10-02 one and the draft
# only renames the publisher. The old guard compared versionId (the draft) with the published id,
# so it refused forever; both are pinned now, and either one moving means re-review.
EXPECTED_ACTIVE = "9e2a1c16-fdd8-4c8c-a1c0-09e32210fd61"
EXPECTED_DRAFT = "336ed21a-cea5-40a9-b1ad-75354360a736"

# Nodes are found by id, never by name: renaming the publisher in the editor is what broke the
# old name-keyed build (KeyError on "Ntfy Topic: transcription-complete").
RECEIVED_TRIGGER = "f3bf0372-0cb4-447f-b0ba-bac7a2f11a6d"
COMPLETION_TRIGGER = "7b344c02-578e-4f44-8665-d5b3f6c44d82"
PUBLISHER = "05297d6b-a06d-4ae7-8bd9-4be42dc8edc3"
COMPLETION_FORMATTER = "d8751fc9-4d6d-48ae-b8ae-b0c164188d71"
# The trigger's durable consumer is named n8n-<workflow>-<node id>; a fixed id keeps its stream
# position across re-runs, where a fresh one would restart at deliver_policy new and drop intakes.
INTAKE_TRIGGER = "878c2d92-85c6-4e31-a41d-4625f7971b8f"
INTAKE_FORMATTER = "51755e20-d8ba-482f-8fef-4e263ee4ced5"
NAMES = {PUBLISHER: "Ntfy JSON Publish (audio)", COMPLETION_FORMATTER: "Format Final Completion",
         INTAKE_TRIGGER: "Ticketable Item", INTAKE_FORMATTER: "Format Intake Item"}
REBUILT = (COMPLETION_FORMATTER, INTAKE_TRIGGER, INTAKE_FORMATTER)
INTAKE_EVENT = "bloodbank.audio.intake.detected"
CREDENTIAL_NAME = "Wax ntfy JSON publisher"
PENDING_CREDENTIAL = "RELEASE_CREATES_HEADER_AUTH_CREDENTIAL"
# The public API's workflowSettings is additionalProperties:false and live settings carry
# binaryMode, so a verbatim PUT is a 400. n8n merges sent settings over stored ones, so a key
# left out here is kept, not deleted.
API_SETTINGS = frozenset({"saveExecutionProgress", "saveManualExecutions", "saveDataErrorExecution",
                          "saveDataSuccessExecution", "executionTimeout", "errorWorkflow", "timezone",
                          "executionOrder", "callerPolicy", "callerIds", "timeSavedPerExecution",
                          "availableInMCP"})


class ApiError(RuntimeError):
    def __init__(self, method, path, status, message):
        super().__init__(f"{method} {path}: HTTP {status} {message}".rstrip())
        self.status = status


def secret(reference):
    return subprocess.check_output(["op", "read", reference], text=True, timeout=20).strip()


def request(path, key, method="GET", payload=None):
    req = urllib.request.Request(BASE + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"X-N8N-API-KEY": key, "Content-Type": "application/json", "User-Agent": "HeyMa-Wax/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            message = str(json.load(error).get("message", ""))[:500]
        except (ValueError, AttributeError):
            message = ""
        raise ApiError(method, path, error.code, message) from None


def code_node(identity, source_file, function, position):
    source = Path(__file__).with_name(source_file).read_text()
    source += f"\nreturn $input.all().map((item,index) => ({{json: {function}(item.json), pairedItem: {{item: index}}}}));\n"
    return {"id": identity, "name": NAMES[identity], "type": "n8n-nodes-base.code", "typeVersion": 2,
            "position": position, "parameters": {"mode": "runOnceForAllItems", "jsCode": source}}


def link(target):
    return {"main": [[{"node": target, "type": "main", "index": 0}]]}


def rename(connections, old, new):
    """Connections are keyed and targeted by node NAME, so a rename must rewrite both."""
    if old != new and old in connections:
        connections[new] = connections.pop(old)
    for outputs in connections.values():
        for branches in outputs.values():
            for targets in branches or []:
                for target in targets or []:
                    if target.get("node") == old:
                        target["node"] = new


def branch(workflow, trigger_id):
    """Every node reachable from a trigger, with its outgoing connections, keyed by node id."""
    by_name = {node["name"]: node for node in workflow.get("nodes", [])}
    start = [node["name"] for node in workflow.get("nodes", []) if node["id"] == trigger_id]
    seen = {}
    while start:
        name = start.pop()
        if name in seen or name not in by_name:
            continue
        seen[name] = by_name[name]
        for branches in workflow.get("connections", {}).get(name, {}).values():
            for targets in branches or []:
                start.extend(target["node"] for target in targets or [])
    return {node["id"]: {"node": node, "connections": workflow.get("connections", {}).get(name)}
            for name, node in seen.items()}


def build(live, credential):
    result = copy.deepcopy(live)
    nodes = {node["id"]: node for node in result["nodes"]}
    for identity in (RECEIVED_TRIGGER, COMPLETION_TRIGGER, PUBLISHER):
        if identity not in nodes:
            raise RuntimeError(f"node identity changed: {identity} is gone")
    if PUBLISHER in branch(live, RECEIVED_TRIGGER):
        raise RuntimeError("the New Audio File branch now reaches the publisher; refusing to touch it")
    for node in result["nodes"]:
        if node["name"] in NAMES.values() and NAMES.get(node["id"]) != node["name"]:
            raise RuntimeError(f"node name {node['name']!r} is taken by {node['id']}")
    connections = result["connections"]
    for node in result["nodes"]:
        if node["id"] in REBUILT:
            connections.pop(node["name"], None)
    result["nodes"] = [node for node in result["nodes"] if node["id"] not in REBUILT]
    publisher = nodes[PUBLISHER]
    rename(connections, publisher["name"], NAMES[PUBLISHER])
    for stale in ("executeOnce", "continueOnFail", "alwaysOutputData"):
        publisher.pop(stale, None)
    # A plain JSON POST that fails the execution on any error: the ntfySend node it replaces
    # swallowed a toDateTime crash behind onError continueRegularOutput and reported success.
    publisher.update(name=NAMES[PUBLISHER], type="n8n-nodes-base.httpRequest", typeVersion=4.2,
                     position=[256, 208], retryOnFail=True, maxTries=3, waitBetweenTries=2000,
                     onError="stopWorkflow", credentials={"httpHeaderAuth": credential}, parameters={
                         "method": "POST", "url": "https://ntfy.delo.sh",
                         "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
                         "sendBody": True, "contentType": "raw", "rawContentType": "application/json",
                         "body": "={{ $json.encoded }}", "options": {"timeout": 30000}})
    result["nodes"] += [
        code_node(COMPLETION_FORMATTER, "format-completion.js", "formatCompletion", [16, 96]),
        {"id": INTAKE_TRIGGER, "name": NAMES[INTAKE_TRIGGER], "type": "n8n-nodes-bloodbank.bloodbankTrigger",
         "typeVersion": 1, "position": [-224, 320], "parameters": {"events": [INTAKE_EVENT], "connection": {}}},
        code_node(INTAKE_FORMATTER, "format-intake.js", "formatIntake", [16, 320]),
    ]
    connections[nodes[COMPLETION_TRIGGER]["name"]] = link(NAMES[COMPLETION_FORMATTER])
    connections[NAMES[COMPLETION_FORMATTER]] = link(NAMES[PUBLISHER])
    connections[NAMES[INTAKE_TRIGGER]] = link(NAMES[INTAKE_FORMATTER])
    connections[NAMES[INTAKE_FORMATTER]] = link(NAMES[PUBLISHER])
    settings = {key: value for key, value in live["settings"].items() if key in API_SETTINGS}
    return {"name": live["name"], "nodes": result["nodes"], "connections": connections, "settings": settings}


def listed_credentials(key):
    found, cursor = [], ""
    while True:
        page = request("/credentials?limit=250" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else ""), key)
        found += page.get("data", [])
        cursor = page.get("nextCursor")
        if not cursor:
            return found


def credential_for(live, key, requested, create):
    """The publisher's Header Auth credential: given, already wired, found by name, else created.

    n8n rewrites a node's credential name to the stored one on save, so the name is carried
    through from wherever the id came from; otherwise every re-run would differ from live.
    """
    publisher = next(node for node in live["nodes"] if node["id"] == PUBLISHER)
    wired = (publisher.get("credentials") or {}).get("httpHeaderAuth") or {}
    if requested:
        name = wired.get("name") if wired.get("id") == requested else CREDENTIAL_NAME
        return {"id": requested, "name": name or CREDENTIAL_NAME}, "given by --credential-id"
    if wired.get("id"):
        return {"id": wired["id"], "name": wired.get("name") or CREDENTIAL_NAME}, "already wired into the publisher"
    try:
        found = [c for c in listed_credentials(key) if c.get("type") == "httpHeaderAuth" and c.get("name") == CREDENTIAL_NAME]
    except ApiError as error:
        if error.status != 403:
            raise
        found = []
        print("credential lookup: this API key may not list credentials (403); reuse one with --credential-id",
              file=sys.stderr)
    if len(found) > 1:
        raise RuntimeError(f"{len(found)} {CREDENTIAL_NAME!r} credentials exist; pick one with --credential-id")
    if found:
        return {"id": found[0]["id"], "name": found[0]["name"]}, "found by name"
    if not create:
        return {"id": PENDING_CREDENTIAL, "name": CREDENTIAL_NAME}, "none found; --apply creates it"
    created = request("/credentials", key, "POST", {
        "name": CREDENTIAL_NAME, "type": "httpHeaderAuth",
        "data": {"name": "Authorization", "value": "Bearer " + secret(NTFY_REF)}})
    print(f"created credential {created['id']}; reuse it with --credential-id if this run fails", file=sys.stderr)
    return {"id": created["id"], "name": created.get("name") or CREDENTIAL_NAME}, "created"


def unchanged(live, candidate):
    return live["nodes"] == candidate["nodes"] and live["connections"] == candidate["connections"]


def summary(live, candidate, credential, how):
    before = {node["id"]: node for node in live["nodes"]}
    after = {node["id"]: node for node in candidate["nodes"]}
    lines = [f"live: published={live.get('activeVersionId')} draft={live.get('versionId')} active={live.get('active')}",
             f"credential: {credential['id']} ({how})"]
    for identity in list(before) + [i for i in after if i not in before]:
        old, new = before.get(identity), after.get(identity)
        state = "removed" if new is None else "added" if old is None else "unchanged" if old == new else "changed"
        name = f"{old['name']} -> {new['name']}" if old and new and old["name"] != new["name"] else (new or old)["name"]
        kind = f"{old['type']} -> {new['type']}" if old and new and old["type"] != new["type"] else (new or old)["type"]
        lines.append(f"  {state:9} {identity} {name} [{kind}]")
    for key in sorted(set(live["connections"]) | set(candidate["connections"])):
        if live["connections"].get(key) != candidate["connections"].get(key):
            lines.append(f"  connection {key!r}: {targets(live['connections'].get(key))} -> {targets(candidate['connections'].get(key))}")
    dropped = sorted(set(live.get("settings") or {}) - set(candidate["settings"]))
    if dropped:
        lines.append(f"  settings not sent (public API rejects them; n8n keeps the stored values): {dropped}")
    if unchanged(live, candidate):
        lines.append("candidate equals the live draft: --apply would change nothing")
    return "\n".join(lines)


def targets(connection):
    return [target["node"] for branches in (connection or {}).values() for group in branches or [] for target in group or []]


def comparable(node):
    """n8n rewrites a node's credential name to the stored one on save; only the id is ours."""
    if not node or not node.get("credentials"):
        return node
    return {**node, "credentials": {kind: {"id": (cred or {}).get("id")} for kind, cred in node["credentials"].items()}}


def snapshot(workflow, trigger_id):
    return {identity: {"node": comparable(entry["node"]), "connections": entry["connections"]}
            for identity, entry in branch(workflow, trigger_id).items()}


def verify(state, candidate, received, version):
    problems = []
    if not state.get("active") or state.get("activeVersionId") != version or state.get("versionId") != version:
        problems.append(f"published={state.get('activeVersionId')} draft={state.get('versionId')} "
                        f"active={state.get('active')}, expected {version}")
    expected = {node["id"]: node for node in candidate["nodes"]}
    for label, view in (("draft", state), ("published", state.get("activeVersion") or {})):
        nodes = {node["id"]: node for node in view.get("nodes", [])}
        problems += [f"{label} node {identity} ({(expected.get(identity) or nodes[identity])['name']}) differs"
                     for identity in sorted(set(nodes) | set(expected))
                     if comparable(nodes.get(identity)) != comparable(expected.get(identity))]
        if view.get("connections") != candidate["connections"]:
            problems.append(f"{label} connections differ")
        if snapshot(view, RECEIVED_TRIGGER) != received:
            problems.append(f"{label} New Audio File branch changed")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="PUT, publish and verify (default: dry run)")
    parser.add_argument("--credential-id", help="Header Auth credential for the publisher (skips find/create)")
    parser.add_argument("--expected-active", default=EXPECTED_ACTIVE,
                        help="live activeVersionId (published version) the candidate was reviewed against; '' = unpublished")
    parser.add_argument("--expected-draft", default=EXPECTED_DRAFT,
                        help="live versionId (draft) the candidate was reviewed against")
    parser.add_argument("--out", type=Path, help="dry run: write the candidate JSON here instead of stdout")
    args = parser.parse_args()
    key = secret(API_REF)
    live = request("/workflows/" + WORKFLOW_ID, key)
    if (live.get("activeVersionId") or "", live.get("versionId")) != (args.expected_active, args.expected_draft):
        raise SystemExit(f"workflow changed since review: published={live.get('activeVersionId')} "
                         f"draft={live.get('versionId')}, expected {args.expected_active} / {args.expected_draft}; "
                         "review the new state, then pass --expected-active/--expected-draft")
    credential, how = credential_for(live, key, args.credential_id, create=args.apply)
    candidate = build(live, credential)
    print(summary(live, candidate, credential, how), file=sys.stderr)
    if not args.apply:
        text = json.dumps(candidate, indent=2) + "\n"
        if args.out:
            args.out.write_text(text)
            print(f"candidate written to {args.out}", file=sys.stderr)
        else:
            sys.stdout.write(text)
        return 0
    received = snapshot(live, RECEIVED_TRIGGER)
    version = live["versionId"] if unchanged(live, candidate) else \
        request("/workflows/" + WORKFLOW_ID, key, "PUT", candidate)["versionId"]
    state = request("/workflows/" + WORKFLOW_ID, key)
    # The public PUT already publishes an active workflow (n8n 2.x publishIfActive); activating
    # the same version again would only bounce every trigger's consumer for nothing.
    if not state.get("active") or state.get("activeVersionId") != version:
        request(f"/workflows/{WORKFLOW_ID}/activate", key, "POST", {"versionId": version})
        state = request("/workflows/" + WORKFLOW_ID, key)
    problems = verify(state, candidate, received, version)
    if problems:
        raise SystemExit(f"version {version} is live but NOT verified: " + "; ".join(problems) +
                         f". Previous published version: {live['activeVersionId']}")
    print(json.dumps({"workflow_id": WORKFLOW_ID, "version_id": version, "credential_id": credential["id"],
                      "previous_published": live["activeVersionId"],
                      "verified": ["completion branch", "intake branch", "New Audio File branch untouched"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
