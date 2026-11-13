import sys, sqlite3, re, os
from pathlib import Path
sys.path.insert(0, "/home/delorenj/HeyMa/.worktrees/hm19-tui-validate/components/wax/src")
from wax import voices
conn = voices.connect()
print("needs_relabel:", voices.needs_relabel(conn))
v = conn.execute("select voice_id,name,status,merged_into,note_path from voices where voice_id='voice-6e760e'").fetchone()
print("voice-6e760e:", dict(v))
for r in conn.execute("select s.item_id, s.label, s.rendered, s.voice_id, s.verdict, t.md_path from voice_sightings s join transcripts t using(item_id) order by 1"):
    txt = Path(r["md_path"]).read_text()
    heads = re.findall(r"^\*\*(.+?) \(([0-9:]+)\):\*\*", txt, re.M)
    fm_voices = re.findall(r"^  - \"?(\[\[.*?\]\])\"?$", txt, re.M)
    print(f"{r['item_id']} sighting label={r['label']!r} rendered={r['rendered']!r} verdict={r['verdict']} | note turn heads={heads} | voices:={fm_voices}")
for r in conn.execute("select item_id, ep_slug, state, attempt, version from passes where ep_slug='voice-id' order by 1"): print("pass row:", dict(r))
