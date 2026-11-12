#!/usr/bin/env python3
"""Monitor, brief and applier for the HeyMa PM pass (heyma-pm).

An interval driver (the interval-driver skill: ~/.agents/skills/interval-driver). A script, not
the model, holds the context:

  pm-pass.py --monitor   stable snapshot: one line per open HEYMA ticket (lane, owner comments
                         waiting on the PM, stale claim) plus one line per FAILING `wax doctor`
                         probe (name only). Hermes hashes it; unchanged = the agent never starts.
  pm-pass.py --brief     the pass contract (pm-pass.prompt.md), the PM's roadmap
                         (runtime/pm-roadmap.md), the waiting owner comments and the failing and
                         warning doctor probes with their fixes, so the pass needs no reads.
  pm-pass.py --apply     applies the agent's plan (runtime/pm-pass.json) with interval-driver's
                         px-apply.py: lease, [pm] tag, allowed verbs, cap, duplicate-comment skip,
                         roadmap write. The agent decides; the driver acts.

Hermes only runs scripts under ~/.hermes/profiles/heyma-pm/scripts/, so pm-monitor.sh and
pm-brief.sh there are one-line execs of this file.
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
RT = HERE / "runtime"
WAX = ROOT / "bin" / "wax"
ET = ZoneInfo("America/New_York")
NOW = datetime.now(timezone.utc)
OPEN = {"backlog", "unstarted", "started"}
# Agent comments carry a tag ("[pm]", "[em]", ...) or the legacy "heyma-pm —" prefix. Every
# Plane comment is posted as the operator's account, so an untagged comment is the operator's.
AGENT = re.compile(r"^(\[[a-z0-9-]+\]|heyma-pm\b|momo\b|hermes\b)", re.I)


def px(*args: str) -> dict:
    out = subprocess.run(["px", *args, "--json"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    return json.loads(out.stdout)


def text(c: dict) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c.get("comment_html") or ""))).strip()


def is_pm(c: dict) -> bool:
    t = text(c)
    return t.startswith("[pm]") or t.startswith("heyma-pm")


def ticket(ref: str) -> dict:
    t = px("task", "get", ref)["ticket"]
    cs = sorted(t.get("comments") or [], key=lambda c: c["created_at"])
    last_pm = max((c["created_at"] for c in cs if is_pm(c)), default="")
    waiting = [c for c in cs if not AGENT.match(text(c)) and c["created_at"] > last_pm]
    labels = t.get("label_names") or []
    return {
        "ref": ref,
        "name": t["name"],
        "lane": t.get("state_name") or "?",
        "group": t.get("state_group"),
        "pri": t.get("priority") or "none",
        "working": "agent:working" in labels,
        "updated": datetime.fromisoformat(t["updated_at"]),
        "waiting": waiting,
    }


def rows() -> list[dict]:
    refs = [f"HEYMA-{i['sequence_id']}" for i in px("task", "list")["issues"] if i.get("state_group") in OPEN]
    with ThreadPoolExecutor(8) as pool:
        return sorted(pool.map(ticket, refs), key=lambda r: int(r["ref"].split("-")[1]))


def doctor() -> list[dict]:
    """Non-passing `wax doctor` probes. Raises when the doctor itself cannot run."""
    out = subprocess.run([str(WAX), "doctor", "--json"], cwd=ROOT, capture_output=True, text=True, timeout=180)
    return [p for p in json.loads(out.stdout)["probes"] if p.get("status") != "pass"]


def stale(r: dict) -> bool:
    return r["working"] and NOW - r["updated"] > timedelta(hours=48)


def monitor() -> None:
    print(f"date {NOW.astimezone(ET):%F}")
    try:
        for r in rows():
            print(f"{r['ref']}|{r['lane']}|owner-waiting:{len(r['waiting'])}|{'stale-claim' if stale(r) else '-'}")
    except Exception:
        print("board UNAVAILABLE")
    try:
        # FAIL only, by probe name: warn probes flap (the inference chain's served_by changes
        # every call) and the detail text carries counts that move on their own.
        for name in sorted({p["probe"] for p in doctor() if p.get("status") == "fail"}):
            print(f"doctor-fail|{name}")
    except Exception:
        print("doctor UNAVAILABLE")


def age(d: datetime) -> str:
    h = (NOW - d).total_seconds() / 3600
    return f"{h:.0f}h" if h < 48 else f"{h / 24:.0f}d"


def brief() -> None:
    print((HERE / "pm-pass.prompt.md").read_text())
    roadmap = RT / "pm-roadmap.md"
    print("# Your roadmap (runtime/pm-roadmap.md, written by your last pass's plan)")
    print(roadmap.read_text() if roadmap.exists() and roadmap.stat().st_size else
          "NO ROADMAP YET: build it this pass from the snapshot below. Do not read the whole board.")
    print(f"\n# PM snapshot {NOW.astimezone(ET):%a %Y-%m-%d %H:%M} ET")
    try:
        rs = rows()
    except Exception as e:
        rs = []
        print(f"Board: UNAVAILABLE ({e})")
    print("\n## Open tickets (ref | lane | priority | age since update | name)")
    for r in rs:
        flag = " STALE-CLAIM" if stale(r) else (" agent:working" if r["working"] else "")
        print(f"- {r['ref']} | {r['lane']} | {r['pri']} | {age(r['updated'])}{flag} | {r['name'][:90]}")
    if not rs:
        print("none")
    print("\n## Owner comments waiting on you (newest last, clipped)")
    waiting = [(r, c) for r in rs for c in r["waiting"]]
    for r, c in waiting:
        print(f"- {r['ref']} [{r['lane']}] {c['created_at'][:16]}: {text(c)[:600]}")
    if not waiting:
        print("none")
    print("\n## wax doctor: probes not passing (fail wakes you; warn is context only)")
    try:
        probes = doctor()
        for p in sorted(probes, key=lambda p: (p["status"] != "fail", p["probe"])):
            print(f"- {p['status'].upper()} {p['probe']}: {p.get('detail', '')[:300]}")
            if p.get("fix"):
                print(f"  fix: {p['fix'][:300]}")
        if not probes:
            print("all pass")
    except Exception as e:
        print(f"doctor UNAVAILABLE ({e})")


def apply() -> None:
    """Apply the pass's plan (runtime/pm-pass.json) through the interval-driver applier."""
    os.execvp(sys.executable, [
        sys.executable, str(Path.home() / ".agents/skills/interval-driver/scripts/px-apply.py"),
        str(RT / "pm-pass.json"), "--cwd", str(ROOT), "--tag", "[pm]",
        "--ops", "comment,move,release,create", "--max", "6",
        "--lease", str(RT / "wip-driver.lock"), "--owner", "hermes:heyma-pm:pm-pass",
        "--roadmap", str(RT / "pm-roadmap.md")])


if __name__ == "__main__":
    {"--monitor": monitor, "--brief": brief, "--apply": apply}[sys.argv[1]]()
