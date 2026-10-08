import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[3]


class DropoffTest(unittest.TestCase):
    def run_isolated(self, script):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                ["python3", "-c", script],
                env={**os.environ, "WAX_ROOT": directory,
                     "PYTHONPATH": str(REPO / "components/wax/src")},
                cwd=REPO, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_copies_only_accepted_audio_and_never_reimports_a_deletion(self):
        self.run_isolated("""
import json
from wax import dropoff,paths,ledger
paths.ensure_dirs()
paths.DROPOFF.mkdir()
policy=paths.VAR/'syncthing-audio'/'received.json'
policy.parent.mkdir()
policy.write_text(json.dumps(['received.mp3']))
source=paths.DROPOFF/'received.mp3'
source.write_bytes(b'complete synthetic recording')
(paths.DROPOFF/'unaccepted.mp3').write_bytes(b'not yet accepted')
(paths.DROPOFF/'.syncthing.partial.mp3.tmp').write_bytes(b'partial')
first=dropoff.copy_received()
assert len(first)==1
assert first[0].read_bytes()==source.read_bytes()
assert source.read_bytes()==b'complete synthetic recording'
assert not (paths.INBOX/'unaccepted.mp3').exists()
assert dropoff.copy_received()==[]
first[0].unlink() # synthetic test bytes only
assert dropoff.copy_received()==[]
assert source.is_file()
row=ledger.connect().execute('select origin,orig_name from items').fetchone()
assert row['origin']=='dropoff' and row['orig_name']=='received.mp3'
""")

    def test_collision_preserves_both_recordings_and_completed_items_stay_done(self):
        self.run_isolated("""
import json
from wax import dropoff,paths,ledger
paths.ensure_dirs()
paths.DROPOFF.mkdir()
policy=paths.VAR/'syncthing-audio'/'received.json'
policy.parent.mkdir()
policy.write_text(json.dumps(['same.mp3','done.mp3']))
(paths.DROPOFF/'same.mp3').write_bytes(b'foreign recording')
(paths.INBOX/'same.mp3').write_bytes(b'local recording')
done=paths.DROPOFF/'done.mp3'
done.write_bytes(b'already processed')
item=ledger.upsert_item(done)
ledger.set_item_state(item,'complete')
result=dropoff.copy_received()
assert len(result)==1 and result[0].name!='same.mp3'
assert result[0].read_bytes()==b'foreign recording'
assert (paths.INBOX/'same.mp3').read_bytes()==b'local recording'
assert not (paths.INBOX/'done.mp3').exists()
assert dropoff.copy_received()==[]
assert done.read_bytes()==b'already processed'
""")

    def test_a_changing_source_never_publishes_an_unverified_copy(self):
        self.run_isolated("""
import json
from unittest.mock import patch
from wax import dropoff,paths,ledger
paths.ensure_dirs()
paths.DROPOFF.mkdir()
policy=paths.VAR/'syncthing-audio'/'received.json'
policy.parent.mkdir()
policy.write_text(json.dumps(['changing.mp3']))
source=paths.DROPOFF/'changing.mp3'
source.write_bytes(b'original')
real_copy=dropoff.shutil.copyfileobj
def changing(src,dst):
 real_copy(src,dst)
 dst.write(b'changed during copy')
with patch.object(dropoff.shutil,'copyfileobj',side_effect=changing):
 assert dropoff.copy_received()==[]
assert not (paths.INBOX/'changing.mp3').exists()
assert source.read_bytes()==b'original'
assert list((paths.INBOX/'.staging').glob('*.part'))
assert ledger.connect().execute('select count(*) from items').fetchone()[0]==0
""")


if __name__ == "__main__":
    unittest.main()
