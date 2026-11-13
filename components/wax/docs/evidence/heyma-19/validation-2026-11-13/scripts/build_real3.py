"""Isolated fixture the REAL voice-id pass can run on, built from COPIES of a few small real items.
Source reads are read-only (sqlite ?mode=ro URI of the production ledger copy made earlier, files copied with cp).
Everything the pass or the TUI may write is under /tmp/heyma-validate-heyma-19/real3."""
import os, re, shutil, sqlite3, sys
from pathlib import Path
D = Path("/tmp/heyma-validate-heyma-19"); R = D / "real3"
SRC_DB = D / "real4/root/var/wax.db"             # sqlite .backup of production, taken read-only earlier
shutil.rmtree(R, ignore_errors=True)
for sub in ("root/var", "vault", "voices", "audio"): (R / sub).mkdir(parents=True)
src = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True); src.row_factory = sqlite3.Row
picked = [r["item_id"] for r in src.execute("""
  select s.item_id from voice_sightings s join items i using(item_id) join transcripts t using(item_id)
  where s.voice_id='voice-6e760e' and s.verdict='matched' and s.turns is not null and s.vec is not null
  and t.md_path like '/home/delorenj/d/Transcripts/%' order by t.audio_duration limit 3""")]
print("picked items:", picked)
shutil.copy(SRC_DB, R / "root/var/wax.db")
db = sqlite3.connect(R / "root/var/wax.db"); db.row_factory = sqlite3.Row
keep = {"voices", "voice_exemplars", "items", "transcripts", "voice_sightings", "passes", "meta", "sqlite_sequence", "pipelines"}
for (t,) in db.execute("select name from sqlite_master where type='table'").fetchall():
    if t in keep: continue
    try: db.execute(f'delete from "{t}"')
    except sqlite3.Error as e: print("skip", t, e)
marks = ",".join("?" for _ in picked)
for t in ("items", "transcripts", "voice_sightings", "passes"):
    db.execute(f"delete from {t} where item_id not in ({marks})", picked)
db.commit()
# the voices notes and clips: a copy, and the registry points at the copy
for f in Path("/home/delorenj/d/Voices").iterdir(): shutil.copy2(f, R / "voices" / f.name)
db.execute("update voices set note_path = replace(note_path, '/home/delorenj/d/Voices', ?)", (str(R / "voices"),))
for row in db.execute("select item_id from items").fetchall():
    item = row["item_id"]
    i = db.execute("select path from items where item_id=?", (item,)).fetchone()
    t = db.execute("select md_path, asr_path from transcripts where item_id=?", (item,)).fetchone()
    audio = R / "audio" / Path(i["path"]).name
    shutil.copy2(i["path"], audio)
    md = R / "vault" / Path(t["md_path"]).name
    shutil.copy2(t["md_path"], md)
    db.execute("update items set path=? where item_id=?", (str(audio), item))
    db.execute("update transcripts set md_path=?, asr_path=NULL where item_id=?", (str(md), item))
db.commit()
db.execute("vacuum")
for t in ("voices", "voice_exemplars", "items", "transcripts", "voice_sightings", "passes"):
    print(t, db.execute(f"select count(*) from {t}").fetchone()[0])
# nothing in the isolated ledger may still point at production
bad = []
for t, cols in (("items", ["path"]), ("transcripts", ["md_path", "asr_path"]), ("voices", ["note_path"])):
    for c in cols:
        for (v,) in db.execute(f"select {c} from {t} where {c} is not null"):
            if not str(v).startswith(str(R)): bad.append((t, c, v))
print("paths outside the isolated root:", bad)
