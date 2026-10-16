#!/usr/bin/python3
"""Read-only Telnyx S3 transport. Wax owns everything after inbox publication."""

import argparse
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[2]
ENDPOINT = "https://s3.delo.sh"
BUCKET = "recordings"
PREFIX = "5c224422-c4be-4c68-9b17-e3848499a198/"
AWS = shutil.which("aws") or "/usr/local/bin/aws"
KEY_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})/([A-Za-z0-9][A-Za-z0-9_-]{0,150})\.mp3", re.ASCII)


class IntakeError(Exception):
    """Only fixed, non-sensitive codes may cross the logging boundary."""


class Interrupted(IntakeError):
    pass


def log(event, **fields):
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Source:
    endpoint: str
    region: str
    bucket: str
    prefix: str

    @property
    def id(self):
        return digest([self.endpoint, self.region, self.bucket, self.prefix])


@dataclass(frozen=True)
class Object:
    key: str
    etag: str
    version: str
    size: int
    modified: str

    def id(self, source):
        return digest([source.id, self.key, self.etag, self.version, self.size, self.modified])


@dataclass(frozen=True)
class Config:
    source: Source
    access_key_ref: str
    secret_key_ref: str
    op: str
    ffprobe: str
    aws: str = AWS
    settle_seconds: int = 120
    max_object_bytes: int = 2**31
    run_seconds: int = 240

    @classmethod
    def load(cls, path):
        data = json.loads(path.read_text())
        allowed = {"endpoint", "region", "bucket", "prefix", "access_key_ref", "secret_key_ref",
                   "op", "ffprobe", "aws", "settle_seconds", "max_object_bytes", "run_seconds"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise IntakeError("config_fields_invalid")
        source = Source(*(data[name] for name in ("endpoint", "region", "bucket", "prefix")))
        # This installation has ONE authorized intake scope. Wax's backup keys
        # must never become a configurable bucket-wide intake by accident.
        if source != Source(ENDPOINT, "us-east-1", BUCKET, PREFIX):
            raise IntakeError("source_scope_not_authorized")
        for field in ("access_key_ref", "secret_key_ref"):
            if not isinstance(data[field], str) or not re.fullmatch(r"op://[^\s\x00-\x1f]+", data[field]):
                raise IntakeError("credential_reference_invalid")
        data.setdefault("aws", AWS)
        for field in ("op", "ffprobe", "aws"):
            if not isinstance(data[field], str) or not Path(data[field]).is_absolute():
                raise IntakeError("executable_path_invalid")
        limits = {"settle_seconds": (120, 30, 86400), "max_object_bytes": (2**31, 1, 2**34),
                  "run_seconds": (240, 10, 240)}
        for name, (default, low, high) in limits.items():
            value = data.get(name, default)
            if type(value) is not int or not low <= value <= high:
                raise IntakeError("config_limit_invalid")
        return cls(source, **{k: v for k, v in data.items()
                              if k not in {"endpoint", "region", "bucket", "prefix"}})


class Deadline:
    def __init__(self, seconds):
        self.end = time.monotonic() + seconds

    def remaining(self, cap):
        left = self.end - time.monotonic()
        if left <= 0:
            raise IntakeError("run_deadline_exceeded")
        return min(left, cap)


def child_env():
    # Shared non-auth environment only. Neither AWS nor ffprobe receives op
    # tokens, ambient credentials, proxy settings, or credential helper options.
    return {key: os.environ[key] for key in ("HOME", "USER", "LANG", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "TMPDIR")
            if key in os.environ} | {"PATH": "/usr/local/bin:/usr/bin:/bin"}


def op_env():
    # The installed wrapper also loads its normal service-account bootstrap and
    # may route reads through Connect. Do not force its bypass mode.
    names = ("OP_SERVICE_ACCOUNT_TOKEN", "OP_CONNECT_HOST", "OP_CONNECT_TOKEN",
             "OP_ACCOUNT", "OP_BIOMETRIC_UNLOCK_ENABLED", "OP_LOAD_DESKTOP_APP_SETTINGS",
             "OP_SESSION_DELEGATION_ENABLED")
    return child_env() | {key: os.environ[key] for key in names if key in os.environ} | {"OP_CACHE": "off"}


def command(argv, env, deadline, cap, code):
    """No shell, credential argv, stderr replay, or orphaned subprocess groups."""
    try:
        process = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        raise IntakeError(code + "_unavailable") from None
    try:
        stdout, _ = process.communicate(timeout=deadline.remaining(cap))
        if process.returncode:
            raise IntakeError(code + "_failed")
        return stdout
    except subprocess.TimeoutExpired:
        raise IntakeError(code + "_timeout") from None
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        process.stdout.close()


def credentials(config, deadline):
    values = []
    for ref in (config.access_key_ref, config.secret_key_ref):
        value = command([config.op, "read", "--no-newline", ref],
                        op_env(), deadline, 20, "credential_read").decode().strip()
        if not value or any(c.isspace() or ord(c) < 32 for c in value):
            raise IntakeError("credential_value_invalid")
        values.append(value)
    return tuple(values)


def object_from_metadata(key, data):
    try:
        etag = data["ETag"].removeprefix('"').removesuffix('"')
        size = data["ContentLength"]
        stamp = datetime.fromisoformat(data["LastModified"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
        modified = stamp.astimezone(timezone.utc).isoformat()
        version = data.get("VersionId", "")
        if version == "null":
            version = ""
        if not re.fullmatch(r"[a-fA-F0-9]{32}(?:-[1-9][0-9]*)?", etag) or type(size) is not int or size < 0:
            raise ValueError()
        if not isinstance(version, str) or len(version) > 1024 or any(ord(c) < 32 for c in version):
            raise ValueError()
        return Object(key, etag, version, size, modified)
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        raise IntakeError("remote_metadata_invalid") from None


class Remote:
    def __init__(self, config, deadline):
        self.config, self.deadline = config, deadline
        access, secret = credentials(config, deadline)
        self.env = child_env() | {
            "AWS_ACCESS_KEY_ID": access, "AWS_SECRET_ACCESS_KEY": secret,
            "AWS_DEFAULT_REGION": config.source.region, "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_PAGER": "", "AWS_MAX_ATTEMPTS": "1", "AWS_CLI_AUTO_PROMPT": "off",
            "AWS_CONFIG_FILE": "/dev/null", "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        }

    def aws(self, args, code, cap=45):
        flags = ["--endpoint-url", self.config.source.endpoint, "--cli-connect-timeout", "10",
                 "--cli-read-timeout", "30", "--output", "json", "--no-cli-pager"]
        raw = command([self.config.aws, *flags, "s3api", *args], self.env, self.deadline, cap, code)
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, TypeError):
            raise IntakeError(code + "_invalid") from None

    def listing(self):
        source = self.config.source
        rows, tokens, token = [], set(), None
        while True:
            args = ["list-objects-v2", "--bucket", source.bucket, "--prefix", source.prefix,
                    "--no-paginate", "--max-keys", "1000"]
            if token:
                args.extend(["--continuation-token", token])
            page = self.aws(args, "remote_list")
            contents = page.get("Contents", [])
            if not isinstance(contents, list) or any(not isinstance(row, dict) for row in contents):
                raise IntakeError("remote_listing_invalid")
            rows.extend(contents)
            if page.get("IsTruncated") is False:
                return rows
            token = page.get("NextContinuationToken")
            if page.get("IsTruncated") is not True or not isinstance(token, str) or not token or token in tokens:
                raise IntakeError("remote_pagination_invalid")
            tokens.add(token)

    def head(self, key):
        source = self.config.source
        if not key.startswith(source.prefix) or not safe_relative(key[len(source.prefix):]):
            raise IntakeError("object_scope_invalid")
        data = self.aws(["head-object", "--bucket", source.bucket, "--key", key], "remote_head")
        return object_from_metadata(key, data)

    def download(self, obj, path):
        source = self.config.source
        if not obj.key.startswith(source.prefix) or not safe_relative(obj.key[len(source.prefix):]):
            raise IntakeError("object_scope_invalid")
        args = ["get-object", "--bucket", source.bucket, "--key", obj.key,
                "--if-match", '"' + obj.etag + '"']
        if obj.version:
            args.extend(["--version-id", obj.version])
        data = self.aws([*args, str(path)], "remote_download", cap=180)
        if object_from_metadata(obj.key, data) != obj:
            raise IntakeError("remote_identity_changed")


def safe_relative(relative):
    if not isinstance(relative, str):
        return False
    match = KEY_PATTERN.fullmatch(relative)
    if not match:
        return False
    try:
        date.fromisoformat(match[1])
    except ValueError:
        return False
    return True


def candidates(config, rows):
    seen = set()
    for row in rows:
        key = row.get("Key")
        size = row.get("Size")
        if not isinstance(key, str) or not key.startswith(config.source.prefix):
            continue
        relative = key[len(config.source.prefix):]
        if (not safe_relative(relative) or
                type(size) is not int or not 0 < size <= config.max_object_bytes):
            continue
        if relative in seen:
            raise IntakeError("remote_listing_duplicate")
        seen.add(relative)
        yield key, size


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IntakeError("overlap_lock_busy") from None
        yield
    finally:
        os.close(fd)


def hashes(path):
    sha, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise IntakeError("local_file_not_regular")
        count = 0
        while chunk := stream.read(1024 * 1024):
            count += len(chunk)
            sha.update(chunk)
            md5.update(chunk)
    return count, sha.hexdigest(), md5.hexdigest()


def probe(path, config, deadline):
    raw = command([config.ffprobe, "-v", "error", "-protocol_whitelist", "file", "-show_entries",
                   "stream=codec_type,codec_name:format=duration", "-of", "json", str(path)],
                  child_env(), deadline, 20, "audio_probe")
    try:
        data = json.loads(raw)
        duration = float(data["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0 or not any(
            s.get("codec_type") == "audio" and s.get("codec_name") == "mp3" for s in data["streams"]
        ):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise IntakeError("audio_probe_invalid") from None


SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, object_key TEXT NOT NULL,
    etag TEXT NOT NULL, version_id TEXT NOT NULL, size INTEGER NOT NULL, modified TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('downloading','prepared','delivered','failed')),
    sha256 TEXT, final_name TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1,
    error_code TEXT, updated_at TEXT NOT NULL, delivered_at TEXT, proof TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    source_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
    outcome TEXT NOT NULL, error_code TEXT, delivered INTEGER NOT NULL DEFAULT 0
);
"""


def now_text():
    return datetime.now(timezone.utc).isoformat()


def wax_has_hash(path, sha256):
    if not path.exists():
        return False
    # Never import Wax or initialize/migrate its schema. mode=ro includes live
    # WAL state; immutable=1 would miss recent consumer receipts.
    try:
        with closing(sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro", uri=True, timeout=2)) as db:
            db.execute("PRAGMA query_only=ON")
            return db.execute("SELECT 1 FROM items WHERE sha256=? LIMIT 1", (sha256,)).fetchone() is not None
    except sqlite3.Error:
        raise IntakeError("wax_readonly_lookup_failed") from None


class Intake:
    def __init__(self, config, root=ROOT):
        self.config = config
        self.root = root
        self.state_dir = root / "var/telnyx-s3-intake"
        self.stage_dir = self.state_dir / "staging"
        self.inbox = root / "inbox"
        self.db_path = self.state_dir / "receipts.db"
        self.wax_db = root / "var/wax.db"

    def initialize(self):
        for path in (self.state_dir, self.stage_dir, self.inbox):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.is_symlink() or not path.is_dir():
                raise IntakeError("runtime_directory_invalid")
            if path != self.inbox and (path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
                raise IntakeError("runtime_directory_not_private")
        if self.db_path.is_symlink():
            raise IntakeError("receipt_database_invalid")
        if self.stage_dir.stat().st_dev != self.inbox.stat().st_dev:
            raise IntakeError("staging_inbox_filesystems_differ")

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=3)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.executescript(SCHEMA)
        sync_dir(self.state_dir)
        return db

    def stage(self, receipt_id):
        if not re.fullmatch(r"[a-f0-9]{64}", receipt_id):
            raise IntakeError("receipt_id_invalid")
        return self.stage_dir / (receipt_id + ".partial")

    def checkpoint(self, phase):
        """Crash-injection seam for hermetic tests; no runtime fault controls."""

    def remove_stage(self, receipt_id):
        path = self.stage(receipt_id)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise IntakeError("staging_entry_invalid")
        path.unlink()
        sync_dir(self.stage_dir)

    def cleanup_staging(self, db):
        errors = []
        for path in self.stage_dir.iterdir():
            if not re.fullmatch(r"[a-f0-9]{64}\.partial", path.name):
                continue
            rid = path.name[:-8]
            row = db.execute("SELECT state FROM receipts WHERE receipt_id=?", (rid,)).fetchone()
            if row and row["state"] == "prepared":
                continue
            try:
                # Includes delivered receipts interrupted after commit and
                # unprepared partial downloads. Unknown filenames stay intact.
                self.remove_stage(rid)
            except IntakeError as exc:
                errors.append(str(exc))
                log("staging_cleanup_failed", receipt=rid, code=str(exc))
        return errors

    def final_name(self, obj):
        relative = obj.key[len(self.config.source.prefix):]
        if not safe_relative(relative):
            raise IntakeError("object_scope_invalid")
        match = KEY_PATTERN.fullmatch(relative)
        return f"telnyx-{match[1]}-{match[2][:72]}-{obj.id(self.config.source)}.mp3"

    def delivered(self, db, row, proof):
        with db:
            db.execute("UPDATE receipts SET state='delivered', delivered_at=?, updated_at=?, error_code=NULL, proof=? WHERE receipt_id=?",
                       (now_text(), now_text(), proof, row["receipt_id"]))
        self.checkpoint("after_receipt")
        self.remove_stage(row["receipt_id"])
        log("delivered", receipt=row["receipt_id"], proof=proof)

    def publish(self, db, row):
        # Recompute names rather than trusting a path loaded from durable state.
        obj = Object(row["object_key"], row["etag"], row["version_id"], row["size"], row["modified"])
        if (row["source_id"] != self.config.source.id or row["receipt_id"] != obj.id(self.config.source)
                or row["final_name"] != self.final_name(obj) or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"] or "")):
            raise IntakeError("prepared_receipt_invalid")
        target, stage = self.inbox / row["final_name"], self.stage(row["receipt_id"])
        try:
            size, sha, _ = hashes(target)
        except FileNotFoundError:
            pass
        else:
            if (size, sha) != (row["size"], row["sha256"]):
                raise IntakeError("publication_collision")
            sync_dir(self.inbox)
            self.delivered(db, row, "inbox_sha256")
            return
        if wax_has_hash(self.wax_db, row["sha256"]):
            self.delivered(db, row, "wax_sha256")
            return
        try:
            size, sha, _ = hashes(stage)
        except FileNotFoundError:
            raise IntakeError("prepared_staging_missing") from None
        if (size, sha) != (row["size"], row["sha256"]):
            raise IntakeError("prepared_staging_corrupt")
        try:
            os.link(stage, target, follow_symlinks=False)
        except FileExistsError:
            raise IntakeError("publication_collision") from None
        sync_dir(self.inbox)
        self.checkpoint("after_publication")
        self.delivered(db, row, "atomic_link")

    def receive(self, db, remote, obj, deadline):
        rid = obj.id(self.config.source)
        row = db.execute("SELECT * FROM receipts WHERE receipt_id=?", (rid,)).fetchone()
        if row and row["state"] == "delivered":
            return False
        if row and row["state"] == "prepared":
            self.publish(db, row)
            return True
        with db:
            db.execute("""INSERT INTO receipts (receipt_id,source_id,object_key,etag,version_id,size,modified,state,final_name,updated_at)
                VALUES(?,?,?,?,?,?,?,'downloading',?,?) ON CONFLICT(receipt_id) DO UPDATE SET
                state='downloading',sha256=NULL,error_code=NULL,attempts=attempts+1,updated_at=excluded.updated_at""",
                       (rid, self.config.source.id, obj.key, obj.etag, obj.version, obj.size,
                        obj.modified, self.final_name(obj), now_text()))
        stage = self.stage(rid)
        try:
            self.remove_stage(rid)
            fd = os.open(stage, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            try:
                original = os.fstat(fd)
                remote.download(obj, stage)
                current = stage.lstat()
                if (not stat.S_ISREG(current.st_mode) or current.st_mode & 0o077 or
                        (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)):
                    raise IntakeError("download_staging_replaced")
                os.fsync(fd)
            finally:
                os.close(fd)
            size, sha, md5 = hashes(stage)
            if not size or size != obj.size:
                raise IntakeError("download_size_mismatch")
            if "-" not in obj.etag and md5 != obj.etag.lower():
                raise IntakeError("download_md5_mismatch")
            probe(stage, self.config, deadline)
            modified = datetime.fromisoformat(obj.modified).timestamp()
            os.utime(stage, (modified, modified), follow_symlinks=False)
            fd = os.open(stage, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            sync_dir(self.stage_dir)
            if remote.head(obj.key) != obj:
                raise IntakeError("remote_identity_changed")
            self.checkpoint("before_prepare")
            with db:
                db.execute("UPDATE receipts SET state='prepared',sha256=?,updated_at=? WHERE receipt_id=?",
                           (sha, now_text(), rid))
            self.checkpoint("after_prepare")
            row = db.execute("SELECT * FROM receipts WHERE receipt_id=?", (rid,)).fetchone()
            self.publish(db, row)
            return True
        except (IntakeError, OSError) as exc:
            row = db.execute("SELECT state FROM receipts WHERE receipt_id=?", (rid,)).fetchone()
            with db:
                db.execute("UPDATE receipts SET state=?,error_code=?,updated_at=? WHERE receipt_id=?",
                           (row[0] if row[0] in {"prepared", "delivered"} else "failed",
                            str(exc) if isinstance(exc, IntakeError) else "local_io_failed", now_text(), rid))
            raise

    def run(self, factory=Remote):
        self.initialize()
        with lock(self.state_dir / "intake.lock"):
            db = self.connect()
            count = 0
            try:
                with db:
                    db.execute("INSERT OR REPLACE INTO runs (source_id,started_at,outcome) VALUES(?,?,'running')",
                               (self.config.source.id, now_text()))
                deadline = Deadline(self.config.run_seconds)
                failures = self.cleanup_staging(db)
                blocked = set()
                prepared = db.execute("SELECT * FROM receipts WHERE source_id=? AND state='prepared' ORDER BY updated_at",
                                      (self.config.source.id,)).fetchall()
                for row in prepared:
                    deadline.remaining(1)
                    try:
                        self.publish(db, row)
                        count += 1
                    except (IntakeError, OSError) as exc:
                        if isinstance(exc, Interrupted):
                            raise
                        # Missing/corrupt private staging can be re-fetched from
                        # S3. A collision or ledger error needs operator review.
                        code = str(exc) if isinstance(exc, IntakeError) else "local_io_failed"
                        recoverable = code in {"prepared_staging_missing", "prepared_staging_corrupt"}
                        current = db.execute("SELECT state FROM receipts WHERE receipt_id=?",
                                             (row["receipt_id"],)).fetchone()[0]
                        with db:
                            db.execute("UPDATE receipts SET state=?,error_code=?,updated_at=? WHERE receipt_id=?",
                                       ("delivered" if current == "delivered" else "failed" if recoverable else "prepared",
                                        code, now_text(), row["receipt_id"]))
                        if not recoverable:
                            blocked.add(row["receipt_id"])
                            failures.append(code)
                            log("object_failed", receipt=row["receipt_id"], code=code)
                remote = factory(self.config, deadline)
                for key, size in list(candidates(self.config, remote.listing())):
                    deadline.remaining(1)
                    try:
                        obj = remote.head(key)
                        if obj.size != size:
                            raise IntakeError("remote_listing_changed")
                        if obj.id(self.config.source) in blocked:
                            continue
                        age = (datetime.now(timezone.utc) - datetime.fromisoformat(obj.modified)).total_seconds()
                        if age < self.config.settle_seconds:
                            continue
                        count += self.receive(db, remote, obj, deadline)
                    except (IntakeError, OSError) as exc:
                        if isinstance(exc, Interrupted):
                            raise
                        code = str(exc) if isinstance(exc, IntakeError) else "local_io_failed"
                        failures.append(code)
                        log("object_failed", object_id=digest(key), code=code)
                # An unresolved prepared artifact that disappeared remotely must
                # not be hidden behind a successful empty list.
                unresolved = db.execute("SELECT 1 FROM receipts WHERE source_id=? AND state!='delivered' LIMIT 1",
                                        (self.config.source.id,)).fetchone()
                if failures:
                    log("run_partial", delivered=count, failed=len(failures))
                    raise IntakeError("object_failures")
                if unresolved:
                    raise IntakeError("undelivered_receipts_remain")
                with db:
                    db.execute("UPDATE runs SET outcome='success',finished_at=?,delivered=? WHERE source_id=?",
                               (now_text(), count, self.config.source.id))
                log("run_complete", delivered=count)
            except BaseException as exc:
                code = str(exc) if isinstance(exc, IntakeError) else "local_or_internal_failure"
                with db:
                    db.execute("UPDATE runs SET outcome='failed',finished_at=?,error_code=?,delivered=? WHERE source_id=?",
                               (now_text(), code, count, self.config.source.id))
                raise
            finally:
                db.close()

    def dry_run(self, factory=Remote):
        deadline = Deadline(self.config.run_seconds)
        remote = factory(self.config, deadline)
        count = 0
        for key, size in candidates(self.config, remote.listing()):
            obj = remote.head(key)
            if obj.size != size:
                raise IntakeError("remote_listing_changed")
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(obj.modified)).total_seconds()
            log("dry_run_object", receipt=obj.id(self.config.source), bytes=obj.size, etag=obj.etag,
                versioned=bool(obj.version), settled=age >= self.config.settle_seconds)
            count += 1
        log("dry_run_complete", candidates=count)

    def status(self):
        if not self.db_path.exists():
            log("status", outcome="never_run")
            return 1
        with closing(sqlite3.connect("file:" + quote(str(self.db_path), safe="/") + "?mode=ro", uri=True, timeout=3)) as db:
            db.row_factory = sqlite3.Row
            run = db.execute("SELECT * FROM runs WHERE source_id=?", (self.config.source.id,)).fetchone()
            counts = dict(db.execute("SELECT state,COUNT(*) FROM receipts WHERE source_id=? GROUP BY state", (self.config.source.id,)))
            errors = [dict(row) for row in db.execute("SELECT receipt_id,state,error_code FROM receipts WHERE source_id=? AND state!='delivered'",
                                                    (self.config.source.id,))]
        log("status", run=dict(run) if run else None, receipts=counts, errors=errors)
        return 0 if run and run["outcome"] == "success" and not errors else 1


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "dry-run", "status"))
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/heyma/telnyx-s3-intake.json")
    args = parser.parse_args(argv)
    def stop(signum, frame):
        raise Interrupted("interrupted")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        intake = Intake(Config.load(args.config))
        if args.action == "status":
            return intake.status()
        if args.action == "dry-run":
            intake.dry_run()
        else:
            intake.run()
        return 0
    except IntakeError as exc:
        log("failure", code=str(exc))
        return 75 if str(exc) == "overlap_lock_busy" else 1
    except Exception:
        # Do not serialize exception messages, response bodies, process env,
        # argv, or tracebacks: dependencies can include authentication data.
        log("failure", code="local_or_internal_failure")
        return 1


if __name__ == "__main__":
    sys.exit(main())
