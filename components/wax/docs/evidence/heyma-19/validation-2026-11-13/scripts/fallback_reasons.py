import os, sys, json, collections
from pathlib import Path
R = "/tmp/heyma-validate-heyma-19/real4"
os.environ.update(WAX_ROOT=R+"/root", WAX_VAULT=R+"/vault", WAX_VOICES_DIR=R+"/voices")
sys.path.insert(0, "/home/delorenj/HeyMa/.worktrees/hm19-tui-validate/components/wax/src")
from wax import voices, voices_tui as t, voiceprint
conn = voices.connect()
reasons = collections.Counter(); per = {}
for r in voices.listing(conn):
    if r["status"] != "unnamed" or r["merged_into"]: continue
    clips = t.plan_clips(conn, r["voice_id"])
    if clips and "listening clip" not in clips[0].what: continue
    sights = conn.execute("select s.item_id,s.turns,s.verdict,s.model,s.domain,i.path from voice_sightings s left join items i on i.item_id=s.item_id where s.voice_id=?", (r["voice_id"],)).fetchall()
    why = set()
    for s in sights:
        if s["model"] != voiceprint.MODEL_NAME: why.add("other-model-sighting")
        elif s["turns"] is None: why.add(f"turns-null({s['domain']})")
        elif s["verdict"] not in t.CONFIDENT: why.add(f"verdict-{s['verdict']}")
        elif not s["path"] or not Path(s["path"]).exists(): why.add("audio-missing")
        else: why.add("turns-too-short-or-parse")
    per[r["voice_id"]] = sorted(why); reasons.update(why)
print(dict(reasons))
for k, v in per.items(): print(k, v)
