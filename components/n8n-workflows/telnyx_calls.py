#!/usr/bin/python3
"""Release the "On Telnyx DeLoSIP Event" n8n workflow (9BkQCtKxdEtPWLAn).

The workflow is the Telnyx webhook's only consumer. It owns nothing: every event goes, untouched,
to `wax calls event` through the n8n-nodes-heyma "Wax Calls" node, and Wax keeps the call log.
Only the event that settles a call (Wax answers `ended: true`, once per call: an outbound hangup,
an answered inbound hangup, or a voicemail landing; never a missed or declined call) is announced: bloodbank.telephony.call.ended plus one ntfy line on topic `telephony`.

Dry run by default (prints the candidate). --apply PUTs it (n8n publishes an active workflow on
PUT) and checks the published version is the one sent. Re-running changes nothing.
"""
import argparse
import json
import sys

from release import API_REF, API_SETTINGS, request, secret

WORKFLOW_ID = "9BkQCtKxdEtPWLAn"
# Ids are kept from the live workflow so the webhook URL and the ntfy credential survive.
WEBHOOK = "1bc7a9e5-1a37-4fe3-b49e-3ac7c0973688"
BLOODBANK = "a6fb150c-9c75-4d5c-82ed-6e6b1b7b2547"
NTFY = "8a5e9c7f-c4a5-4a2a-a301-622d7197666b"
WAX_CALLS = "5b0c3f7e-3c1a-4d55-9a0e-7a51c0de0001"
ENDED = "5b0c3f7e-3c1a-4d55-9a0e-7a51c0de0002"

# Wax outcome -> the bloodbank disposition enum.
DISPOSITION = ("({answered: 'answered', voicemail: 'voicemail', missed: 'missed', declined: 'rejected', "
               "no_answer: 'no_answer', failed: 'failed'})[$json.call.outcome] || 'failed'")
ENDED_DATA = (
    "={{ { call_id: $json.call_session_id, line: $json.call.line || '+12154498437',"
    " direction: $json.call.direction,"
    " from: ($json.call.direction === 'inbound' ? $json.call.remote_number : $json.call.line) || 'anonymous',"
    " to: ($json.call.direction === 'inbound' ? $json.call.line : $json.call.remote_number) || 'anonymous',"
    f" disposition: {DISPOSITION},"
    " duration_seconds: $json.call.talk_seconds || 0, hangup_cause: $json.call.hangup_cause || null,"
    " ended_at: $json.call.ended_at,"
    " remote_name: $json.call.remote_name || null, forwarded_from: $json.call.forwarded_from || null,"
    " forward_reason: $json.call.forward_reason || null } }}"
)
NTFY_MESSAGE = (
    "={{ ($json.call.direction === 'inbound' ? '← ' : '→ ') + ($json.call.remote_name || $json.call.remote_number"
    " || 'anonymous') + ' · ' + $json.call.outcome + ($json.call.talk_seconds ? ' · ' + $json.call.talk_seconds"
    " + 's' : '') + ($json.call.forward_reason ? ' · fwd ' + $json.call.forward_reason : '') }}"
)


def build(live):
    by_id = {node["id"]: node for node in live["nodes"]}
    webhook, bloodbank, ntfy = by_id[WEBHOOK], dict(by_id[BLOODBANK]), dict(by_id[NTFY])
    bloodbank.update(position=[704, -96], parameters={
        "event": "bloodbank.telephony.call.ended", "data": ENDED_DATA,
        "orderingKey": "=call:{{ $json.call_session_id }}", "connection": {}})
    ntfy.update(position=[704, 96], parameters={
        "topic": "telephony", "title": "Call", "message": NTFY_MESSAGE})
    nodes = [
        webhook,
        {"id": WAX_CALLS, "name": "Wax Calls", "type": "n8n-nodes-heyma.waxCalls", "typeVersion": 1,
         "position": [224, 0], "parameters": {"operation": "event", "eventSource": "item"}},
        {"id": ENDED, "name": "Call Ended?", "type": "n8n-nodes-base.if", "typeVersion": 2.2,
         "position": [464, 0], "parameters": {
             "conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict",
                                        "version": 2},
                            "conditions": [{"id": "ended", "leftValue": "={{ $json.ended }}", "rightValue": "",
                                            "operator": {"type": "boolean", "operation": "true",
                                                         "singleValue": True}}],
                            "combinator": "and"},
             "options": {}}},
        bloodbank,
        ntfy,
    ]
    connections = {
        webhook["name"]: {"main": [[{"node": "Wax Calls", "type": "main", "index": 0}]]},
        "Wax Calls": {"main": [[{"node": "Call Ended?", "type": "main", "index": 0}]]},
        "Call Ended?": {"main": [[{"node": bloodbank["name"], "type": "main", "index": 0},
                                  {"node": ntfy["name"], "type": "main", "index": 0}], []]},
    }
    settings = {key: value for key, value in live["settings"].items() if key in API_SETTINGS}
    return {"name": live["name"], "nodes": nodes, "connections": connections, "settings": settings}


def same(live, candidate):
    strip = lambda nodes: sorted((json.dumps({k: n.get(k) for k in ("id", "type", "parameters")},
                                             sort_keys=True) for n in nodes))
    return strip(live["nodes"]) == strip(candidate["nodes"]) and live["connections"] == candidate["connections"]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="PUT and verify (default: dry run)")
    args = parser.parse_args()
    key = secret(API_REF)
    live = request("/workflows/" + WORKFLOW_ID, key)
    candidate = build(live)
    if not args.apply:
        sys.stdout.write(json.dumps(candidate, indent=2) + "\n")
        print("unchanged" if same(live, candidate) else "would change", file=sys.stderr)
        return 0
    if same(live, candidate) and live.get("active"):
        print(json.dumps({"workflow_id": WORKFLOW_ID, "version_id": live["activeVersionId"], "changed": False}))
        return 0
    version = request("/workflows/" + WORKFLOW_ID, key, "PUT", candidate)["versionId"]
    state = request("/workflows/" + WORKFLOW_ID, key)
    if not state.get("active") or state.get("activeVersionId") != version:
        request(f"/workflows/{WORKFLOW_ID}/activate", key, "POST", {"versionId": version})
        state = request("/workflows/" + WORKFLOW_ID, key)
    if state.get("activeVersionId") != version or not same(state, candidate):
        raise SystemExit(f"version {version} is NOT the published one (active {state.get('activeVersionId')})")
    print(json.dumps({"workflow_id": WORKFLOW_ID, "version_id": version, "changed": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
