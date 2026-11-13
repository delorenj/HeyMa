"""Gap 5 sweep: run the split detector over COPIES of many real archived recordings and compare each verdict
with an independent ground truth, the ASR transcript already in the vault (read-only; never a detector input).

A boundary is a FALSE POSITIVE when the transcript shows a speaker turn starting well AFTER the proposed cut.
Reads production: the ledger (mode=ro), archive audio (copied to the sweep dir first) and ~/d/Transcripts (read).
"""
import glob, json, os, re, shutil, sqlite3, sys
sys.path.insert(0, os.environ["WAX_COMPONENT_SRC"])
from wax import split

SWEEP = split.Path(os.environ["HEYMA_VALIDATE"]) / "real-sweep"
SWEEP.mkdir(exist_ok=True)
MIN_B, MAX_B = int(sys.argv[1]), int(sys.argv[2])          # byte window (default 2 MB .. 40 MB)
LIMIT = int(sys.argv[3]) if len(sys.argv) > 3 else 80
con = sqlite3.connect("file:/home/delorenj/HeyMa/var/wax.db?mode=ro", uri=True)
rows = [r for r in con.execute("select item_id,path from items where state in ('complete','enrichment_pending')")
        if "/archive/" in r[1] and "/duplicates/" not in r[1] and os.path.isfile(r[1]) and MIN_B <= os.path.getsize(r[1]) <= MAX_B]
rows.sort(key=lambda r: r[0])           # stable, not hand-picked: item-id order
vault = {}
for p in glob.glob("/home/delorenj/d/Transcripts/*.md"):
    try:
        head = open(p, errors="replace").read(4000)
    except OSError:
        continue
    m = re.search(r"wax-item-id:\s*([0-9a-f]{16})", head)
    if m:
        vault[m.group(1)] = p
TURN = re.compile(r"^\*\*[^*]+?\s\((?:(\d+):)?(\d+):(\d\d)\):\*\*", re.M)
print(f"{len(rows)} candidate recordings in the window; examining the first {LIMIT} that have a transcript")
print(f"{'item':16} {'dur':>7} {'verdict':28} {'cut_s':>7} {'last_turn_s':>11} {'after_cut_s':>11}  note")
done = fp = found = 0
results = []
for item_id, path in rows:
    if done >= LIMIT:
        break
    note = vault.get(item_id)
    if not note:
        continue
    txt = open(note, errors="replace").read()
    turns = [int(a or 0) * 3600 + int(b) * 60 + int(c) for a, b, c in TURN.findall(txt)]
    if not turns:
        continue
    dst = SWEEP / f"{item_id}{split.Path(path).suffix}"
    if not dst.exists():
        shutil.copy2(path, dst)
    try:
        v = split.analyze_file(dst, split.Params())
    except Exception as exc:  # noqa: BLE001
        print(f"{item_id} analysis error: {exc}")
        continue
    done += 1
    last = max(turns)
    results.append((item_id, v['duration_s'], last, v['found']))
    if v["found"]:
        found += 1
        cut = v["boundary"]["cut_s"]
        after = last - cut
        flag = "FALSE-POSITIVE (speech turns start after the cut)" if after > 60 else "ok (no speech after the cut)"
        fp += after > 60
        print(f"{item_id:16} {v['duration_s']:7.0f} {'BOUNDARY':28} {cut:7.0f} {last:11d} {after:11.0f}  {flag}")
    else:
        print(f"{item_id:16} {v['duration_s']:7.0f} {'no ('+v['reason']+')':28} {'-':>7} {last:11d} {'-':>11}")
print(f"\nexamined {done}; boundaries proposed {found}; false positives {fp}")
tails = [r for r in results if r[1] - r[2] > 600]
print(f"recordings whose transcript ends 600+ s before the audio does (candidate overruns): {len(tails)}; "
      f"of those the detector proposed a boundary for {sum(1 for r in tails if r[3])}")
for r in tails:
    print(f"   {r[0]} audio {r[1]:.0f}s, last speech turn {r[2]}s, tail {r[1]-r[2]:.0f}s, boundary: {'yes' if r[3] else 'no'}")
