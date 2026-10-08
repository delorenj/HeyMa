#!/usr/bin/python3
import argparse
import copy
import json
from pathlib import Path
import subprocess
import urllib.request

WORKFLOW_ID = "XHWRrrYCzu4ifdS7"
BASE = "https://n8n.delo.sh/api/v1"
API_REF = "op://DeLoSecrets/5pyy6ct5pi7eaz436g2qkn6pjq/mfiofpxjhlpubqwdzop3ty3eq4"
NTFY_REF = "op://DeLoSecrets/y2bcg7okwrpucnecdswlpcviti/credential"
EXPECTED_VERSION = "9e2a1c16-fdd8-4c8c-a1c0-09e32210fd61"


def secret(reference):
    return subprocess.check_output(["op", "read", reference], text=True, timeout=20).strip()


def request(path, key, method="GET", payload=None):
    req = urllib.request.Request(BASE + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"X-N8N-API-KEY": key, "Content-Type": "application/json", "User-Agent": "HeyMa-Wax/1.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def build(workflow, credential_id):
    result = copy.deepcopy(workflow)
    nodes = {node["name"]: node for node in result["nodes"]}
    for name, identity in {"New Audio File": "f3bf0372-0cb4-447f-b0ba-bac7a2f11a6d",
                           "Transcription Complete": "7b344c02-578e-4f44-8665-d5b3f6c44d82"}.items():
        if nodes.get(name, {}).get("id") != identity:
            raise RuntimeError(f"trigger identity changed: {name}")
    formatter_name = "Format Final Completion"
    source = Path(__file__).with_name("format-completion.js").read_text()
    source += "\nreturn $input.all().map((item,index) => ({json: formatCompletion(item.json), pairedItem: {item: index}}));\n"
    formatter = {"id": "d8751fc9-4d6d-48ae-b8ae-b0c164188d71", "name": formatter_name,
                 "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [16, 96],
                 "parameters": {"mode": "runOnceForAllItems", "jsCode": source}}
    publisher = nodes["Ntfy Topic: transcription-complete"]
    publisher.update(type="n8n-nodes-base.httpRequest", typeVersion=4.2, position=[240, 96],
                     retryOnFail=True, maxTries=3, waitBetweenTries=2000, onError="stopWorkflow")
    publisher.pop("executeOnce", None)
    publisher["parameters"] = {
        "method": "POST", "url": "https://ntfy.delo.sh", "authentication": "genericCredentialType",
        "genericAuthType": "httpHeaderAuth", "sendBody": True, "contentType": "raw",
        "rawContentType": "application/json", "body": "={{ $json.encoded }}",
        "options": {"timeout": 30000},
    }
    publisher["credentials"] = {"httpHeaderAuth": {"id": credential_id, "name": "Wax ntfy JSON publisher"}}
    result["nodes"] = [node for node in result["nodes"] if node["name"] != formatter_name] + [formatter]
    result["connections"]["Transcription Complete"] = {"main": [[{"node": formatter_name, "type": "main", "index": 0}]]}
    result["connections"][formatter_name] = {"main": [[{"node": publisher["name"], "type": "main", "index": 0}]]}
    return {key: result[key] for key in ("name", "nodes", "connections", "settings")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--credential-id")
    parser.add_argument("--expected-version", default=EXPECTED_VERSION)
    args = parser.parse_args()
    key = secret(API_REF)
    live = request("/workflows/" + WORKFLOW_ID, key)
    if live["versionId"] != args.expected_version:
        raise RuntimeError("workflow changed since reviewed version; refusing replacement")
    if not args.apply:
        result = build(live, args.credential_id or "RELEASE_REQUIRES_HEADER_AUTH_CREDENTIAL")
        print(json.dumps(result, indent=2))
        return 0
    credential_id = args.credential_id
    if not credential_id:
        credential = request("/credentials", key, "POST", {
            "name": "Wax ntfy JSON publisher", "type": "httpHeaderAuth",
            "data": {"name": "Authorization", "value": "Bearer " + secret(NTFY_REF)}})
        credential_id = credential["id"]
    candidate = build(live, credential_id)
    updated = request("/workflows/" + WORKFLOW_ID, key, "PUT", candidate)
    received = next(node for node in live["nodes"] if node["name"] == "New Audio File")
    original_branch = live["connections"]["New Audio File"]
    verified = request("/workflows/" + WORKFLOW_ID, key)
    if (next(node for node in verified["nodes"] if node["name"] == "New Audio File") != received
            or verified["connections"]["New Audio File"] != original_branch):
        raise RuntimeError("received branch changed unexpectedly")
    request("/workflows/" + WORKFLOW_ID + "/activate", key, "POST", {"versionId": updated["versionId"]})
    print(json.dumps({"workflow_id": WORKFLOW_ID, "version_id": updated["versionId"], "credential_id": credential_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
