"""Read-only level profile + detection for a COPY of a real recording (HEYMA-18 validation, gap 5).

usage: real-profile.py FILE   (run through harness/iso; imports the worktree's wax.split)
Prints: duration, the detect() verdict, and a coarse level profile (loudest 1 s block per bucket,
share of 1 s blocks above the activity threshold per bucket) so the verdict can be judged against the audio.
"""
import json, os, sys
sys.path.insert(0, os.environ["WAX_COMPONENT_SRC"])
import numpy as np
from wax import split

path = split.Path(sys.argv[1])
frames, secs, _ = split.decode_levels([path])
params = split.Params()
verdict = split.detect(frames, secs, params)
bd = frames.copy()
pad = (-len(bd)) % split.BLOCK_FRAMES
if pad:
    bd = np.concatenate((bd, np.full(pad, split.DB_FLOOR, dtype=bd.dtype)))
blocks = bd.reshape(-1, split.BLOCK_FRAMES).max(axis=1)
thr = verdict["stats"]["threshold_db"]
print(f"file {path.name}  duration {secs:.1f}s ({secs/3600:.2f} h)  blocks {len(blocks)}")
print("stats:", json.dumps(verdict["stats"]))
if verdict["found"]:
    print("VERDICT: boundary", json.dumps(verdict["boundary"]))
else:
    print(f"VERDICT: no boundary [{verdict['reason']}] {verdict['detail']}")
print("longest quiet runs (start-end, s):", [(r['start_s'], r['end_s'], r['length_s']) for r in verdict["quiet_runs"]])
n = 40 if secs > 1800 else 24
step = int(np.ceil(len(blocks) / n))
print(f"profile: {n} buckets of {step}s;  columns: start  max_dBFS  median_dBFS  active%  bar(active share)")
for i in range(0, len(blocks), step):
    b = blocks[i:i + step]
    act = float((b > thr).mean())
    t = i
    print(f"  {t//3600:d}:{(t%3600)//60:02d}:{t%60:02d}  {b.max():7.1f}  {np.median(b):7.1f}  {act*100:5.1f}%  {'#' * int(round(act * 30))}")
tail = blocks[-600:] if len(blocks) > 600 else blocks
print(f"last {len(tail)}s: max {tail.max():.1f} dBFS, median {np.median(tail):.1f} dBFS, active {float((tail > thr).mean())*100:.1f}%")
