"""Hermetic transport tests. No real credentials, audio, network, or Wax DB."""
import contextlib
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / 'intake.py'
spec = importlib.util.spec_from_file_location('telnyx_intake', MODULE_PATH)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
SOURCE = m.Source(m.ENDPOINT, 'us-east-1', m.BUCKET, m.PREFIX)
STAMP = '2026-01-01T00:00:00+00:00'
DATA = b'hermetic mp3 stand-in: the fake decoder accepts this fixture'


def object_for(name='call', data=DATA, **changes):
    return replace(m.Object(SOURCE.prefix + '2026-01-01/' + name + '.mp3',
                            hashlib.md5(data).hexdigest(), '', len(data), STAMP), **changes)


class FakeRemote:
    def __init__(self, objects=None):
        self.objects = list(objects if objects is not None else [object_for()])
        self.downloads = []
        self.heads = []
        self.download_hook = None
        self.head_hook = None
        self.rows = None

    def listing(self):
        return self.rows if self.rows is not None else [dict(Key=o.key, Size=o.size) for o in self.objects]

    def head(self, key):
        self.heads.append(key)
        obj = next(o for o in self.objects if o.key == key)
        return self.head_hook(obj) if self.head_hook else obj

    def download(self, obj, path):
        self.downloads.append(obj)
        if self.download_hook:
            self.download_hook(obj, path)
        else:
            path.write_bytes(DATA)


class Crash(BaseException):
    pass


class TransportFixture:
    def setUp(self):
        self.old_umask = os.umask(0o077)
        self.temp = tempfile.TemporaryDirectory(prefix='telnyx-test-')
        self.root = Path(self.temp.name)
        self.probe = self.root / 'ffprobe'
        self.probe.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"streams":[{"codec_type":"audio","codec_name":"mp3"}],"format":{"duration":"1.25"}}\'\n')
        self.probe.chmod(0o700)
        self.config = m.Config(SOURCE, 'op://test/item/accessKey', 'op://test/item/secretKey',
                               '/unused/op', str(self.probe), aws='/unused/aws')
        self.intake = m.Intake(self.config, self.root)
        self.remote = FakeRemote()
        self.logs = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.logs)
        self.redirect.__enter__()

    def tearDown(self):
        self.redirect.__exit__(None, None, None)
        self.temp.cleanup()
        os.umask(self.old_umask)

    def run_intake(self, remote=None, intake=None):
        (intake or self.intake).run(lambda *_: remote or self.remote)

    def receipt(self, obj=None):
        rid = (obj or object_for()).id(self.config.source)
        with contextlib.closing(sqlite3.connect(self.intake.db_path)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM receipts WHERE receipt_id=?', (rid,)).fetchone()
            return dict(row) if row else None

    def assert_failed(self, code='object_failures'):
        with self.assertRaisesRegex(m.IntakeError, '^' + code + '$'):
            self.run_intake()
        self.assertEqual(self.intake.status(), 1)

    def assert_empty_inbox(self):
        self.assertEqual(list(self.intake.inbox.glob('*')), [])

    def prepare(self):
        with patch.object(self.intake, 'checkpoint', side_effect=lambda p: (_ for _ in ()).throw(Crash()) if p == 'after_prepare' else None):
            with self.assertRaises(Crash):
                self.run_intake()
        self.assertEqual(self.receipt()['state'], 'prepared')


class TransportCase(TransportFixture, unittest.TestCase):
    def test_duplicate_run_and_restart_after_consumer_drain(self):
        self.run_intake()
        target = next(self.intake.inbox.iterdir())
        self.assertEqual(target.read_bytes(), DATA)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(target.stat().st_mtime, datetime.fromisoformat(STAMP).timestamp())
        self.run_intake()
        target.unlink()  # Simulate Wax draining the inbox; no ledger edits.
        self.run_intake(intake=m.Intake(self.config, self.root))
        self.assertEqual(len(self.remote.downloads), 1)
        self.assertEqual(self.receipt()['state'], 'delivered')
        self.assert_empty_inbox()
        self.assertEqual(self.intake.status(), 0)

    def test_crash_boundaries_replay_without_losing_audio(self):
        for phase, state, count in [('before_prepare', 'downloading', 2),
                                    ('after_prepare', 'prepared', 1),
                                    ('after_publication', 'prepared', 1),
                                    ('after_receipt', 'delivered', 1)]:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(dir=self.root) as sub:
                intake = m.Intake(self.config, Path(sub))
                remote = FakeRemote()
                def fail(point):
                    if point == phase:
                        raise Crash()
                with patch.object(intake, 'checkpoint', side_effect=fail):
                    with self.assertRaises(Crash):
                        self.run_intake(remote, intake)
                with contextlib.closing(sqlite3.connect(intake.db_path)) as db:
                    self.assertEqual(db.execute('SELECT state FROM receipts').fetchone()[0], state)
                self.run_intake(remote, m.Intake(self.config, Path(sub)))
                self.assertEqual(len(remote.downloads), count)
                self.assertEqual(len(list(intake.inbox.iterdir())), 1)
                self.assertEqual(list(intake.stage_dir.iterdir()), [])

    def test_crash_after_link_then_consumer_drains_reconciles_readonly(self):
        with patch.object(self.intake, 'checkpoint', side_effect=lambda p: (_ for _ in ()).throw(Crash()) if p == 'after_publication' else None):
            with self.assertRaises(Crash):
                self.run_intake()
        next(self.intake.inbox.iterdir()).unlink()
        with patch.object(m, 'wax_has_hash', return_value=True) as lookup:
            self.run_intake(intake=m.Intake(self.config, self.root))
        lookup.assert_called_with(self.intake.wax_db, hashlib.sha256(DATA).hexdigest())
        self.assertEqual(self.receipt()['proof'], 'wax_sha256')
        self.assertEqual(len(self.remote.downloads), 1)
        self.assert_empty_inbox()

    def test_crash_after_link_and_unrecorded_consumer_may_republish(self):
        with patch.object(self.intake, 'checkpoint', side_effect=lambda p: (_ for _ in ()).throw(Crash()) if p == 'after_publication' else None):
            with self.assertRaises(Crash):
                self.run_intake()
        next(self.intake.inbox.iterdir()).unlink()
        self.run_intake()
        self.assertEqual(len(self.remote.downloads), 1)
        self.assertEqual(len(list(self.intake.inbox.iterdir())), 1)

    def test_delivered_receipt_commit_orphan_cleanup_after_drain(self):
        with patch.object(self.intake, 'checkpoint', side_effect=lambda p: (_ for _ in ()).throw(Crash()) if p == 'after_receipt' else None):
            with self.assertRaises(Crash):
                self.run_intake()
        self.assertEqual(self.receipt()['state'], 'delivered')
        self.assertTrue(self.intake.stage(object_for().id(SOURCE)).exists())
        next(self.intake.inbox.iterdir()).unlink()
        self.run_intake()
        self.assertEqual(list(self.intake.stage_dir.iterdir()), [])
        self.assert_empty_inbox()
        self.assertEqual(len(self.remote.downloads), 1)

    def test_replay_cleanup_failure_preserves_committed_delivery(self):
        self.prepare()
        remove_stage = self.intake.remove_stage
        def fail_delivered_cleanup(rid):
            if self.receipt()['state'] == 'delivered':
                raise m.IntakeError('staging_entry_invalid')
            remove_stage(rid)
        with patch.object(self.intake, 'remove_stage', side_effect=fail_delivered_cleanup):
            self.assert_failed()
        self.assertEqual(self.receipt()['state'], 'delivered')
        next(self.intake.inbox.iterdir()).unlink()
        self.run_intake()
        self.assert_empty_inbox()
        self.assertEqual(list(self.intake.stage_dir.iterdir()), [])
        self.assertEqual(len(self.remote.downloads), 1)

    def test_interrupt_during_prepared_replay_stops_before_remote_work(self):
        self.prepare()
        with patch.object(self.intake, 'publish', side_effect=m.Interrupted('interrupted')):
            with patch.object(self.remote, 'listing') as listing:
                self.assert_failed('interrupted')
                listing.assert_not_called()
        self.assertEqual(self.receipt()['state'], 'prepared')
        self.assert_empty_inbox()
        self.run_intake()
        self.assertEqual(self.receipt()['state'], 'delivered')

    def test_prepared_missing_or_corrupt_stage_refetches(self):
        for corrupt in [False, True]:
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory(dir=self.root) as sub:
                intake = m.Intake(self.config, Path(sub))
                remote = FakeRemote()
                with patch.object(intake, 'checkpoint', side_effect=lambda p: (_ for _ in ()).throw(Crash()) if p == 'after_prepare' else None):
                    with self.assertRaises(Crash):
                        self.run_intake(remote, intake)
                path = intake.stage(object_for().id(SOURCE))
                path.write_bytes(b'broken') if corrupt else path.unlink()
                self.run_intake(remote, intake)
                self.assertEqual(len(remote.downloads), 2)
                self.assertEqual(next(intake.inbox.iterdir()).read_bytes(), DATA)

    def test_unresolved_prepared_source_disappears_not_success(self):
        self.prepare()
        self.intake.stage(object_for().id(SOURCE)).unlink()
        self.remote.objects = []
        self.assert_failed('undelivered_receipts_remain')
        self.assertEqual(self.receipt()['state'], 'failed')

    def test_collision_preserves_both_files_and_does_not_starve_next(self):
        self.intake.initialize()
        target = self.intake.inbox / self.intake.final_name(object_for())
        target.write_bytes(b'preexisting collision')
        self.remote.objects.append(object_for('later'))
        self.assert_failed()
        self.assertEqual(target.read_bytes(), b'preexisting collision')
        self.assertEqual(self.receipt()['state'], 'prepared')
        self.assertEqual(self.receipt(object_for('later'))['state'], 'delivered')
        self.assertTrue(self.intake.stage(object_for().id(SOURCE)).exists())
        self.assertIn('publication_collision', self.logs.getvalue())
        target.unlink()  # Operator resolves collision in this temporary root.
        self.run_intake()
        self.assertEqual(self.receipt()['state'], 'delivered')

    def test_orphans_only_recognized_private_regular_names_removed(self):
        self.intake.initialize()
        orphan = self.intake.stage('a' * 64)
        orphan.write_bytes(b'old interrupted bytes')
        unknown = self.intake.stage_dir / 'operator-notes.partial'
        unknown.write_bytes(b'keep me')
        self.run_intake()
        self.assertFalse(orphan.exists())
        self.assertEqual(unknown.read_bytes(), b'keep me')

    def test_symlink_or_public_orphan_not_removed(self):
        self.intake.initialize()
        victim = self.root / 'victim'
        victim.write_bytes(b'keep')
        link = self.intake.stage('a' * 64)
        link.symlink_to(victim)
        public = self.intake.stage('b' * 64)
        public.write_bytes(b'keep too')
        public.chmod(0o644)
        self.assert_failed()
        self.assertTrue(link.is_symlink())
        self.assertEqual(victim.read_bytes(), b'keep')
        self.assertTrue(public.exists())
        self.assertEqual(self.receipt()['state'], 'delivered')

    def test_interrupted_download_never_publishes_and_is_retryable(self):
        def interrupted(obj, path):
            path.write_bytes(DATA[:3])
            raise m.Interrupted('interrupted')
        self.remote.download_hook = interrupted
        self.assert_failed('interrupted')
        self.assert_empty_inbox()
        self.assertEqual(self.receipt()['state'], 'failed')
        self.remote.download_hook = None
        self.run_intake()
        self.assertEqual(self.receipt()['attempts'], 2)

    def test_size_hash_and_zero_byte_fail_closed(self):
        for content, code in [(b'', 'download_size_mismatch'), (DATA[:-1], 'download_size_mismatch'),
                              (DATA + b'x', 'download_size_mismatch'), (b'X' * len(DATA), 'download_md5_mismatch')]:
            with self.subTest(code=code, length=len(content)):
                self.remote.download_hook = lambda obj, path: path.write_bytes(content)
                self.assert_failed()
                self.assert_empty_inbox()
                self.assertEqual(self.receipt()['error_code'], code)
        self.remote.download_hook = None
        self.run_intake()
        self.assertEqual(self.receipt()['attempts'], 5)

    def test_success_without_any_download_bytes_fails(self):
        self.remote.download_hook = lambda *_: None
        self.assert_failed()
        self.assertEqual(self.receipt()['error_code'], 'download_size_mismatch')
        self.assert_empty_inbox()

    def test_stage_replacement_and_symlink_rejected(self):
        for symlink in [False, True]:
            def replace_stage(obj, path):
                path.unlink()
                path.symlink_to(self.probe) if symlink else path.write_bytes(DATA)
            with self.subTest(symlink=symlink):
                self.remote.download_hook = replace_stage
                self.assert_failed()
                self.assert_empty_inbox()
                self.assertEqual(self.receipt()['error_code'], 'download_staging_replaced')
                self.intake.stage(object_for().id(SOURCE)).unlink()

    def test_changed_post_head_rejected_before_prepare(self):
        self.remote.head_hook = lambda obj: replace(obj, version='new-version') if self.remote.downloads else obj
        self.assert_failed()
        self.assertEqual(self.receipt()['error_code'], 'remote_identity_changed')
        self.assert_empty_inbox()

    def test_listing_size_change_rejected(self):
        self.remote.rows = [dict(Key=object_for().key, Size=len(DATA) + 1)]
        self.assert_failed()
        self.assertEqual(self.remote.downloads, [])
        self.assert_empty_inbox()

    def test_changed_version_etag_size_get_distinct_receipts(self):
        self.run_intake()
        original = object_for()
        self.remote.objects = [replace(original, version='v2')]
        self.run_intake()
        self.remote.objects = [replace(original, etag='1' * 32 + '-2')]
        self.run_intake()
        with contextlib.closing(sqlite3.connect(self.intake.db_path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM receipts WHERE state="delivered"').fetchone()[0], 3)
        self.assertEqual(len(list(self.intake.inbox.iterdir())), 3)
        self.assertNotEqual(original.id(SOURCE), replace(original, size=original.size + 1).id(SOURCE))

    def test_multipart_etag_uses_size_probe_and_local_sha256(self):
        obj = object_for(etag='a' * 32 + '-2')
        self.remote.objects = [obj]
        self.run_intake()
        self.assertEqual(self.receipt(obj)['sha256'], hashlib.sha256(DATA).hexdigest())

    def test_probe_invalid_or_nonzero_never_publishes(self):
        for script, code in [('printf \'{}\'', 'audio_probe_invalid'), ('exit 7', 'audio_probe_failed')]:
            self.probe.write_text('#!/bin/sh\n' + script + '\n')
            self.assert_failed()
            self.assertEqual(self.receipt()['error_code'], code)
            self.assert_empty_inbox()

    def test_permanent_corruption_does_not_starve_later_object(self):
        good = object_for('later')
        self.remote.objects.append(good)
        self.remote.download_hook = lambda obj, path: path.write_bytes(b'bad' if obj.key == object_for().key else DATA)
        self.assert_failed()
        self.assertEqual(self.receipt(good)['state'], 'delivered')
        self.assertEqual(len(self.remote.downloads), 2)
        self.assert_failed()
        self.assertEqual(len(self.remote.downloads), 3)

    def test_remote_listing_failure_nonzero(self):
        with patch.object(self.remote, 'listing', side_effect=m.IntakeError('remote_list_failed')):
            self.assert_failed('remote_list_failed')
        self.assert_empty_inbox()

    def test_lock_bounded_and_prevents_all_remote_work(self):
        self.intake.initialize()
        start = time.monotonic()
        with m.lock(self.intake.state_dir / 'intake.lock'):
            with patch.object(self.remote, 'listing') as listing:
                self.assert_failed('overlap_lock_busy')
                listing.assert_not_called()
        self.assertLess(time.monotonic() - start, 1)

    def test_source_namespace_partitions_identity(self):
        obj = object_for()
        for other in [replace(SOURCE, endpoint='https://elsewhere.invalid'), replace(SOURCE, bucket='other'),
                      replace(SOURCE, prefix='other/'), replace(SOURCE, region='other')]:
            self.assertNotEqual(SOURCE.id, other.id)
            self.assertNotEqual(obj.id(SOURCE), obj.id(other))
        self.run_intake()
        other_config = replace(self.config, source=replace(SOURCE, region='elsewhere'))
        self.run_intake(intake=m.Intake(other_config, self.root))
        self.assertEqual(len(self.remote.downloads), 2)

    def test_allowlist_filters_malicious_keys_and_sidecars(self):
        unsafe = ['../call.mp3', '/2026-01-01/call.mp3', '2026-01-01/../call.mp3',
                  '2026-01-01/a/b.mp3', '2026-99-99/call.mp3', '2026-01-01/.call.mp3',
                  '2026-01-01/call.mp3.partial', '2026-01-01/call.json', '2026-01-01/call.MP3',
                  '2026-01-01/call%2f.mp3', '2026-01-01/call?.mp3', '2026-01-01/call\n.mp3',
                  '2026-01-01/--option.mp3', '2026-01-01/call\\x.mp3']
        rows = [dict(Key=SOURCE.prefix + key, Size=len(DATA)) for key in unsafe]
        rows.extend([dict(Key='2026-01-01/wax-backup.mp3', Size=len(DATA)),
                     dict(Key=SOURCE.prefix + '2026-01-01/zero.mp3', Size=0),
                     dict(Key=SOURCE.prefix + '2026-01-01/huge.mp3', Size=2**40),
                     dict(Key=SOURCE.prefix + '2026-01-01/bool.mp3', Size=True)])
        self.remote.rows = rows
        self.run_intake()
        self.assertEqual(self.remote.heads, [])
        self.assert_empty_inbox()

    def test_duplicate_listing_fails_before_delivery(self):
        self.remote.rows = [dict(Key=object_for().key, Size=len(DATA))] * 2
        self.assert_failed('remote_listing_duplicate')
        self.assertEqual(self.remote.downloads, [])

    def test_unsettled_object_deferred(self):
        self.remote.objects = [object_for(modified=m.now_text())]
        self.run_intake()
        self.assertEqual(self.remote.downloads, [])
        self.assertEqual(self.intake.status(), 0)

    def test_dry_run_creates_no_runtime_files(self):
        self.intake.dry_run(lambda *_: self.remote)
        self.assertFalse(self.intake.state_dir.exists())
        self.assertFalse(self.intake.inbox.exists())
        self.assertEqual(self.remote.downloads, [])
        self.assertNotIn(object_for().key, self.logs.getvalue())

    def test_never_run_and_running_status_nonzero(self):
        self.assertEqual(self.intake.status(), 1)
        self.assertFalse(self.intake.state_dir.exists())
        self.intake.initialize()
        db = self.intake.connect()
        with db:
            db.execute('INSERT INTO runs (source_id,started_at,outcome) VALUES(?,?,?)', (SOURCE.id, STAMP, 'running'))
        db.close()
        self.assertEqual(self.intake.status(), 1)

    def test_readonly_wax_lookup_absent_never_creates_database(self):
        self.assertFalse(m.wax_has_hash(self.intake.wax_db, 'a' * 64))
        self.assertFalse(self.intake.wax_db.exists())

    def test_private_runtime_and_no_symlink_directories(self):
        self.intake.initialize()
        self.intake.stage_dir.chmod(0o755)
        with self.assertRaisesRegex(m.IntakeError, 'runtime_directory_not_private'):
            self.run_intake()
        self.intake.stage_dir.chmod(0o700)
        self.intake.stage_dir.rmdir()
        self.intake.stage_dir.symlink_to(self.root)
        with self.assertRaisesRegex(m.IntakeError, 'runtime_directory_invalid'):
            self.run_intake()

    def test_tampered_prepared_name_cannot_escape_inbox(self):
        self.prepare()
        with contextlib.closing(sqlite3.connect(self.intake.db_path)) as db, db:
            db.execute('UPDATE receipts SET final_name=?', ('../escaped.mp3',))
        self.assert_failed()
        self.assertFalse((self.root / 'escaped.mp3').exists())
        self.assert_empty_inbox()


# Exercises actual Popen argv, JSON parsing, pagination and named-file behavior.
FAKE_AWS = r'''#!/usr/bin/python3
import hashlib, json, os, pathlib, sys
base = pathlib.Path(__file__).parent
fixture = json.loads((base / 'scenario.json').read_text())
args = sys.argv[1:]
operation = args[args.index('s3api') + 1]
def flag(name): return args[args.index(name) + 1]
assert os.environ['AWS_ACCESS_KEY_ID'] == 'fake-access'
assert os.environ['AWS_SECRET_ACCESS_KEY'] == 'fake-secret'
assert os.environ['AWS_DEFAULT_REGION'] == 'us-east-1'
assert os.environ['AWS_MAX_ATTEMPTS'] == '1'
assert os.environ['AWS_EC2_METADATA_DISABLED'] == 'true'
assert os.environ['AWS_PAGER'] == ''
assert os.environ['AWS_CONFIG_FILE'] == '/dev/null'
assert os.environ['AWS_SHARED_CREDENTIALS_FILE'] == '/dev/null'
assert not any(k.startswith(('OP_', 'RCLONE_', 'HTTP_', 'HTTPS_', 'ALL_PROXY', 'NO_PROXY')) for k in os.environ)
assert 'AWS_PROFILE' not in os.environ and 'AWS_SESSION_TOKEN' not in os.environ
with (base / 'audit.jsonl').open('a') as out:
    out.write(json.dumps({'argv': args, 'env_names': sorted(os.environ)}) + '\n')
if fixture.get('error') == operation:
    sys.stderr.write('fake-secret https://secret.invalid/private raw error\n')
    sys.exit(23)
if fixture.get('invalid_json'):
    print('not json')
    sys.exit(0)
key = fixture['key']
data = fixture['data'].encode()
meta = {'ETag': '"' + fixture.get('etag', hashlib.md5(data).hexdigest()) + '"',
        'ContentLength': len(data), 'LastModified': fixture['modified']}
if fixture.get('version'): meta['VersionId'] = fixture['version']
if operation == 'list-objects-v2':
    assert flag('--prefix') == fixture['prefix']
    assert '--no-paginate' in args
    if fixture.get('pages') and '--continuation-token' not in args:
        print(json.dumps({'Contents': [], 'IsTruncated': True, 'NextContinuationToken': 'page-2'}))
    else:
        print(json.dumps({'Contents': [{'Key': key, 'Size': len(data)}], 'IsTruncated': False}))
elif operation == 'head-object':
    assert flag('--key') == key
    print(json.dumps(meta))
elif operation == 'get-object':
    assert flag('--key') == key
    assert flag('--if-match') == meta['ETag']
    assert not fixture.get('version') or flag('--version-id') == fixture['version']
    path = pathlib.Path(args[-1])
    assert path.exists() and path.suffix == '.partial'
    if not fixture.get('no_write'): path.write_bytes(data)
    print(json.dumps(meta))
else: sys.exit(24)
'''


class AWSCase(TransportFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.aws = self.root / 'aws'
        self.aws.write_text(FAKE_AWS)
        self.aws.chmod(0o700)
        self.op = self.root / 'op'
        self.op.write_text('#!/bin/sh\ncase "$3" in *accessKey) printf fake-access ;; *) printf fake-secret ;; esac\n')
        self.op.chmod(0o700)
        self.config = replace(self.config, aws=str(self.aws), op=str(self.op))
        self.intake = m.Intake(self.config, self.root)
        self.scenario = dict(key=object_for().key, prefix=SOURCE.prefix, data=DATA.decode(), modified=STAMP)
        self.write_scenario()

    def write_scenario(self):
        (self.root / 'scenario.json').write_text(json.dumps(self.scenario))

    def live_fake_run(self):
        self.intake.run()

    def test_aws_cli_flags_credentials_and_pagination(self):
        self.scenario.update(pages=True, version='version-2')
        self.write_scenario()
        polluted = {'AWS_ACCESS_KEY_ID': 'wrong', 'AWS_SESSION_TOKEN': 'wrong', 'AWS_PROFILE': 'wrong',
                    'AWS_ENDPOINT_URL': 'https://wrong.invalid', 'HTTPS_PROXY': 'https://wrong.invalid',
                    'HTTP_PROXY': 'https://wrong.invalid', 'ALL_PROXY': 'https://wrong.invalid',
                    'NO_PROXY': '*', 'RCLONE_CONFIG': '/wrong', 'OP_CONNECT_TOKEN': 'op-private'}
        with patch.dict(os.environ, polluted):
            self.live_fake_run()
        audit = [json.loads(line) for line in (self.root / 'audit.jsonl').read_text().splitlines()]
        self.assertEqual([a['argv'][a['argv'].index('s3api') + 1] for a in audit],
                         ['list-objects-v2', 'list-objects-v2', 'head-object', 'get-object', 'head-object'])
        for entry in audit:
            args = entry['argv']
            self.assertEqual(args[args.index('--endpoint-url') + 1], m.ENDPOINT)
            self.assertEqual(args[args.index('--cli-connect-timeout') + 1], '10')
            self.assertEqual(args[args.index('--cli-read-timeout') + 1], '30')
            self.assertEqual(args[args.index('--output') + 1], 'json')
            self.assertIn('--no-cli-pager', args)
            self.assertNotIn('fake-access', json.dumps(args))
            self.assertNotIn('fake-secret', json.dumps(args))
            self.assertEqual(args[args.index('--bucket') + 1], m.BUCKET)
        self.assertEqual(next(self.intake.inbox.iterdir()).read_bytes(), DATA)
        self.assertNotIn('fake-secret', self.logs.getvalue())

    def test_aws_success_no_file_does_not_publish(self):
        self.scenario['no_write'] = True
        self.write_scenario()
        with self.assertRaisesRegex(m.IntakeError, 'object_failures'):
            self.live_fake_run()
        self.assert_empty_inbox()
        self.assertEqual(self.receipt()['error_code'], 'download_size_mismatch')

    def test_aws_nonzero_errors_suppressed(self):
        for operation in ['list-objects-v2', 'head-object', 'get-object']:
            with self.subTest(operation=operation):
                self.scenario['error'] = operation
                self.write_scenario()
                with self.assertRaises(m.IntakeError):
                    self.live_fake_run()
                self.assert_empty_inbox()
                self.assertNotIn('fake-secret', self.logs.getvalue())
                self.assertNotIn('secret.invalid', self.logs.getvalue())
                self.assertEqual(self.intake.status(), 1)

    def test_aws_invalid_json_fails(self):
        self.scenario['invalid_json'] = True
        self.write_scenario()
        with self.assertRaisesRegex(m.IntakeError, 'remote_list_invalid'):
            self.live_fake_run()
        self.assert_empty_inbox()

    def test_op_auth_only_reaches_op_process(self):
        with patch.dict(os.environ, {'OP_SERVICE_ACCOUNT_TOKEN': 'sensitive-op', 'OP_CONNECT_TOKEN': 'connect',
                                     'OP_CONNECT_HOST': 'http://connect.invalid', 'OP_REAL_BIN': '/wrong',
                                     'OP_WRAPPER_CHECK': '1', 'AWS_PROFILE': 'wrong', 'HTTPS_PROXY': 'wrong'}):
            env = m.op_env()
            self.assertEqual(env['OP_SERVICE_ACCOUNT_TOKEN'], 'sensitive-op')
            self.assertEqual(env['OP_CONNECT_TOKEN'], 'connect')
            self.assertNotIn('OP_CONNECT_BYPASS', env)
            self.assertNotIn('OP_REAL_BIN', env)
            self.assertNotIn('OP_WRAPPER_CHECK', env)
            self.assertNotIn('HTTPS_PROXY', env)
            self.assertNotIn('AWS_PROFILE', env)
            self.assertFalse(any(key.startswith('OP_') for key in m.child_env()))

    def test_real_process_exit_crash_boundaries(self):
        for phase in ['before_prepare', 'after_prepare', 'after_publication', 'after_receipt']:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(dir=self.root) as sub:
                root = Path(sub)
                code = '''
import importlib.util, os, pathlib, sys
spec = importlib.util.spec_from_file_location('child_intake', sys.argv[1])
m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m; spec.loader.exec_module(m)
os.umask(0o077)
config = m.Config(m.Source(m.ENDPOINT, 'us-east-1', m.BUCKET, m.PREFIX),
                  'op://test/item/accessKey', 'op://test/item/secretKey', sys.argv[3], sys.argv[4], aws=sys.argv[5])
intake = m.Intake(config, pathlib.Path(sys.argv[2]))
intake.checkpoint = lambda phase: os._exit(31) if phase == sys.argv[6] else None
intake.run()
'''
                process = subprocess.run([sys.executable, '-c', code, str(MODULE_PATH), str(root),
                                          str(self.op), str(self.probe), str(self.aws), phase],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
                self.assertEqual(process.returncode, 31, process.stderr.decode())
                intake = m.Intake(self.config, root)
                intake.run()
                self.assertEqual(next(intake.inbox.iterdir()).read_bytes(), DATA)
                self.assertEqual(list(intake.stage_dir.iterdir()), [])
                self.assertEqual(intake.status(), 0)


class ConfigurationCase(unittest.TestCase):
    def test_example_config_and_absolute_aws(self):
        config = m.Config.load(MODULE_PATH.parent / 'config.example.json')
        self.assertEqual(config.source, SOURCE)
        self.assertTrue(Path(config.aws).is_absolute())

    def test_config_rejects_broader_source_plaintext_credentials_and_bad_limits(self):
        data = json.loads((MODULE_PATH.parent / 'config.example.json').read_text())
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            for changes in [dict(prefix=''), dict(prefix='2026-01-01/'), dict(bucket='other'),
                            dict(endpoint='https://other.invalid'), dict(access_key_ref='plaintext'),
                            dict(aws='aws'), dict(run_seconds=241), dict(settle_seconds=0), dict(extra=True)]:
                with self.subTest(changes=changes):
                    path.write_text(json.dumps(data | changes))
                    with self.assertRaises(m.IntakeError):
                        m.Config.load(path)

    def test_metadata_requires_real_etag_size_timestamp(self):
        good = dict(ETag='"' + 'a' * 32 + '"', ContentLength=5, LastModified=STAMP, VersionId='null')
        self.assertEqual(m.object_from_metadata(object_for().key, good).version, '')
        for changes in [dict(ETag='missing'), dict(ContentLength=True), dict(LastModified='2026-01-01'),
                        dict(VersionId='bad\nvalue'), dict(ContentLength=-1)]:
            with self.subTest(changes=changes), self.assertRaises(m.IntakeError):
                m.object_from_metadata(object_for().key, good | changes)

    def test_command_timeout_and_missing_executable_have_fixed_codes(self):
        with self.assertRaisesRegex(m.IntakeError, '^remote_test_unavailable$'):
            m.command(['/does-not-exist'], m.child_env(), m.Deadline(1), 1, 'remote_test')
        with self.assertRaisesRegex(m.IntakeError, '^remote_test_timeout$'):
            m.command([sys.executable, '-c', 'import time; time.sleep(10)'], m.child_env(), m.Deadline(1), .05, 'remote_test')

    def test_cli_status_never_run_exit_code(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(m, 'ROOT', Path(temp)):
            # Intake default root is bound at definition; patch construction explicitly.
            intake = m.Intake(m.Config.load(MODULE_PATH.parent / 'config.example.json'), Path(temp))
            with patch.object(m, 'Intake', return_value=intake), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m.main(['status', '--config', str(MODULE_PATH.parent / 'config.example.json')]), 1)


if __name__ == '__main__':
    unittest.main()
