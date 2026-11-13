import os, sys, json, collections, time
R = "/tmp/heyma-validate-heyma-19/real4"
os.environ.update(WAX_ROOT=R+"/root", WAX_VAULT=R+"/vault", WAX_VOICES_DIR=R+"/voices", WAX_VOICE_NOTIFY="0")
sys.path.insert(0, "/home/delorenj/HeyMa/.worktrees/hm19-tui-validate/components/wax/src")
from wax import voices, voices_tui as t
from pathlib import Path
print("WAX_ROOT/VAR:", t.paths.VAR, "| DB:", t.paths.DB, "| VOICES_DIR:", voices.VOICES_DIR, "| clip_dir:", t.clip_dir())
conn = voices.connect()
rows = voices.listing(conn)
print("voices.listing rows:", len(rows), collections.Counter(r["status"] + ("/merged" if r["merged_into"] else "") for r in rows))
kinds = collections.Counter(); detail = []
do_cut = len(sys.argv) > 1 and sys.argv[1] == "cut"
bad_cut = 0; cut_total = 0
for r in t.sort_rows(rows):
    if r["status"] != "unnamed" or r["merged_into"]: continue
    clips = t.plan_clips(conn, r["voice_id"])
    if not clips: kind = "none"
    elif clips[0].source.endswith(".ogg") and Path(clips[0].source).parent.name in ("Voices",) or "listening clip" in clips[0].what: kind = "ogg-fallback"
    else: kind = "turn-derived"
    kinds[kind] += 1
    srcs = sorted({Path(c.source).name for c in clips})
    detail.append((r["voice_id"], r["items"], round((r["seconds"] or 0)/60,1), kind, len(clips), len({c.source for c in clips})))
    if do_cut:
        for c in clips:
            p = t.cut_clip(c); cut_total += 1
            if p is None: bad_cut += 1; continue
            d = t.probe_seconds(p)
            if d is None or abs(d - c.dur) > 0.15: bad_cut += 1; print("  DUR MISMATCH", r["voice_id"], c, d)
print("unnamed voices by clip plan:", dict(kinds), "of", sum(kinds.values()))
print("voice_id items minutes kind clips recordings")
for d in detail: print(*d)
if do_cut: print(f"cut {cut_total} clips, {bad_cut} failed or wrong length; wavs on disk:", len(list(t.clip_dir().glob('*.wav'))))
