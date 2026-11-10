"""`wax vault sweep` / `unsweep`: the vault-wide tidy moves the user's own files, so every rule is a test.

A tmp Obsidian vault (``.obsidian/``, ``Transcripts/`` as ``WAX_VAULT``, ``Inbox/``) and a tmp ledger; the
suite-wide sandbox in conftest.py keeps the real vault out of reach. Covers the matchers, every action under
``--apply``, every hard exclusion, the link guard, dry-run purity, the journal written BEFORE each move, the
re-check under the run lock, unsweep, the run lock and the CLI's exit codes.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from wax import documents, frontmatter, hygiene, ledger, paths, pipelines, rename, sentinel, vault_sweep

COMPONENT = Path(__file__).resolve().parents[1]
DAY = 86400.0
OLD = 30 * DAY
# a note nobody wrote anything into: blank body, only an auto stamp and a pipeline status
STAMP = "---\ncreated: 2026-01-01\npipeline-status: new\n---\n"
REAL_TOUCHED = vault_sweep._touched


def _close_test_connection() -> None:
    connection = getattr(ledger._local, "conn", None)
    if connection is not None:
        connection.close()
        del ledger._local.conn


class Env:
    def __init__(self, root: Path):
        self.root = root
        self.vault = root / "vault"
        (self.vault / ".obsidian").mkdir(parents=True)
        (self.vault / "Transcripts").mkdir()
        (self.vault / "Inbox").mkdir()
        self.runtime = root / "wax-root"
        self.var = self.runtime / "var"
        self.var.mkdir(parents=True)

    def put(self, rel: str, text: str = "x", age: float = OLD, data: bytes = None) -> Path:
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if data is not None:
            path.write_bytes(data)
        else:
            path.write_text(text)
        when = time.time() - age
        os.utime(path, (when, when))
        return path

    def age_dirs(self, age: float = OLD) -> None:
        when = time.time() - age
        for dirpath, dirnames, _ in os.walk(self.vault):
            for d in dirnames:
                os.utime(Path(dirpath) / d, (when, when))

    def trash(self, rel: str) -> Path:
        return self.vault / ".trash" / rel

    def snapshot(self) -> dict:
        out = {}
        for dirpath, dirnames, filenames in os.walk(self.vault):
            for name in dirnames + filenames:
                p = Path(dirpath) / name
                st = os.lstat(p)
                out[str(p)] = (st.st_mode, st.st_size, st.st_mtime_ns,
                               p.read_bytes() if p.is_file() and not p.is_symlink() else b"")
        return out


@pytest.fixture
def env():
    tmp = tempfile.TemporaryDirectory()
    e = Env(Path(os.path.realpath(tmp.name)))
    stack = [patch.object(paths, "DB", e.var / "wax.db"), patch.object(paths, "VAR", e.var),
             patch.object(paths, "VAULT", e.vault / "Transcripts"),
             # a test cannot backdate ctime, so the fixture ages files by mtime alone; the ctime rule has its own test
             patch.object(vault_sweep, "_touched", lambda st: st.st_mtime),
             patch.dict(os.environ, {"WAX_VAULT_SWEEP_EXCLUDE": "agents"})]
    for p in stack:
        p.start()
    for name in ("WAX_VAULT_SWEEP_MIN_AGE_S", "WAX_VAULT_SWEEP_TEMP_MIN_AGE_S", "WAX_VAULT_SWEEP_MAX_BYTES"):
        os.environ.pop(name, None)
    _close_test_connection()
    try:
        yield e
    finally:
        for p in reversed(stack):
            p.stop()
        _close_test_connection()
        tmp.cleanup()


class FakePipeline:
    def __init__(self):
        self.calls: list[Path] = []
        self.slugs: dict[str, object] = {}
        self.mode = "complete"

    def __call__(self, name, path, *, binding_id="", doc_id=None, explicit=False):
        assert name == hygiene.UNTITLED_PIPELINE and explicit
        path = Path(path)
        self.calls.append(path)
        row, _ = documents.register(path, binding_id="", origin=f"pipeline:{name}", adopt_doc_id=doc_id)
        if self.mode == "fail":
            return {"failed": 1, "results": [{"state": "failed", "reason_code": "provider_down"}]}
        frontmatter.merge(path, {"summary": "A summary.", "title-slug": self.slugs.get(path.name, "a-great-title")})
        documents.note_own_write("", row["doc_id"], path)
        return {"failed": 0, "completed": 2, "results": [{"state": "completed"}]}


@pytest.fixture
def fake_pipeline():
    fake = FakePipeline()
    with patch.object(pipelines, "run_document", fake), \
            patch.object(pipelines, "compiled", lambda: {hygiene.UNTITLED_PIPELINE: {"definition": {}}}):
        yield fake


def outcomes(summary: dict, action: str) -> dict:
    return {item["path"]: item["outcome"] for item in summary["actions"][action]}


# ------------------------------------------------------------------ matchers --

def test_temp_matcher_table():
    kinds = {"tmp.md": "name", "tmep.md": "name", "temp 2.md": "name", "TMP_3.txt": "name",
             "tmpab12cd34.json": "name", "scratch.md": "name", "scratchpad-2.md": "name",
             "project-scratch.md": "name", "a_scratch.md": "name",
             "components.csv.bak": "backup", "notes.md.bak-20260101": "backup", "x.md.orig": "backup",
             "draft.md~": "dropping", "a.swp": "dropping", "dl.crdownload": "dropping", "video.part": "dropping",
             "x.tmp": "dropping"}
    for name, kind in kinds.items():
        assert vault_sweep.temp_match(name) and vault_sweep.temp_match(name)[0] == kind, name
    for name in ("Template.md", "temperature.md", "tmpl.md", "scratching the surface.md", "bakery.md",
                 "contemporary.md", "attempt.md", ".bak", "Temporal workflows.md", "tmp folder notes.md",
                 # real titles that merely start or end like a temp name
                 "Build a Compiler from Scratch.md", "Learning Scratch.md", "Sourdough from scratch.md",
                 "Tmpanalysis.md", "temp.v2-design.md", "Notes on scratch-built amps.md", "tmpanalysis.md"):
        assert vault_sweep.temp_match(name) is None, name
    assert vault_sweep.backup_original("a.md.bak-2026") == "a.md" and vault_sweep.backup_original("a.md") is None


def test_untitled_matcher_table():
    for stem in ("Untitled", "untitled 2", "Untitled-3", "untitled_12", "UNTITLED CHAT 2024-12-01 15 18 32",
                 "UNTITLED CHAT 2024-12-01 15 18 32 1", "Untitled Diagram", "Untitled canvas 4"):
        assert vault_sweep.UNTITLED_STEM_RE.match(stem), stem
    for stem in ("Untitled Goose Game", "My Untitled", "untitle", "Untitled-", "Untitled thoughts on AI"):
        assert not vault_sweep.UNTITLED_STEM_RE.match(stem), stem


def test_copy_marker_table():
    assert vault_sweep.copy_marker("Plan copy") == ("Plan", "copy")
    assert vault_sweep.copy_marker("Plan copy 2") == ("Plan", "copy")
    assert vault_sweep.copy_marker("Resume (1)") == ("Resume", "paren")
    assert vault_sweep.copy_marker("Braindump 1") == ("Braindump", "n")
    assert vault_sweep.copy_marker("file.sync-conflict-20260101-123456-ABCDEFG") == ("file", "conflict")
    assert vault_sweep.copy_marker("Doc (Jarad's conflicted copy 2026-01-01)") == ("Doc", "conflict")
    for stem in ("July 18", "Jan 09 2025", "Pasted image 20240101123456", "Plan", "Q (100)", "Copy"):
        assert vault_sweep.copy_marker(stem) is None, stem


def test_stamp_only_and_defaults():
    assert vault_sweep.stamp_only("")
    assert vault_sweep.stamp_only("---\ncreated: 2026-01-01\npipeline-status: new\n---\n\n")
    assert vault_sweep.stamp_only("---\nsource: vault-migration\nmigrated_at: x\n---\n")
    assert not vault_sweep.stamp_only("---\ntitle: Real\n---\n")
    assert not vault_sweep.stamp_only("---\ncreated: x\n---\nbody")
    assert not vault_sweep.stamp_only("---\nsource: phone\n---\n")
    assert vault_sweep.default_base("views:\n  - type: table\n    name: Table\n")
    assert not vault_sweep.default_base("views:\n  - type: table\n    name: T\n    filters: {and: []}\n")
    assert not vault_sweep.default_base("filters: {}\n")
    assert vault_sweep.empty_canvas('{"nodes":[],"edges":[]}') and vault_sweep.empty_canvas("")
    assert not vault_sweep.empty_canvas('{"nodes":[{"id":"a"}],"edges":[]}')


def test_link_keys():
    assert vault_sweep._link_key("Notes/tmp.md") == "tmp"
    assert vault_sweep._link_key("<Inbox/My%20Note.md#head>") == "my note"
    assert vault_sweep._link_key("https://example.com/tmp.md") == ""
    assert vault_sweep._link_key("#anchor") == ""
    assert vault_sweep._link_key("tmp11\\") == "tmp11"  # `[[tmp11\|alias]]` inside a table


# ------------------------------------------------------------------- actions --

def test_dry_run_changes_nothing_and_writes_no_manifest(env, fake_pipeline):
    env.put("Notes/empty.md", "")
    env.put("Notes/tmp.md", STAMP)
    env.put("Notes/Untitled.md", "real words")
    env.put("Notes/Plan.md", "same")
    env.put("Notes/Plan copy.md", "same")
    env.put("Loose.md", "root note")
    (env.vault / "Notes" / "Empty" / "deeper").mkdir(parents=True)
    env.age_dirs()
    before = env.snapshot()
    out = vault_sweep.sweep()
    assert out["dry_run"] and out["run"] is None and out["errors"] == []
    assert env.snapshot() == before
    assert not vault_sweep.manifest_path().exists()
    assert fake_pipeline.calls == []
    assert outcomes(out, "prune-empty") == {"Notes/empty.md": "would_trash"}
    assert outcomes(out, "prune-temp") == {"Notes/tmp.md": "would_trash"}
    assert outcomes(out, "untitled") == {"Notes/Untitled.md": "would_title"}
    assert outcomes(out, "consolidate") == {"Notes/Plan copy.md": "would_trash"}
    assert outcomes(out, "organize") == {"Loose.md": "would_move", "Notes/Empty/deeper": "would_remove",
                                         "Notes/Empty": "would_remove"}


def test_prune_empty(env):
    env.put("Notes/zero.png", data=b"")
    env.put("Notes/blank.md", "---\ntags: []\n---\n  \n")
    env.put("Notes/real.md", "hello")
    env.put("Notes/fresh.md", "", age=60)
    out = vault_sweep.sweep(apply=True, only=["prune-empty"])
    assert out["errors"] == []
    assert env.trash("Notes/zero.png").exists() and env.trash("Notes/blank.md").exists()
    assert (env.vault / "Notes/real.md").exists() and (env.vault / "Notes/fresh.md").exists()
    assert outcomes(out, "prune-empty")["Notes/fresh.md"] == "held"


def test_prune_temp_age_floor_and_link_guard(env):
    env.put("Notes/tmp.md", STAMP, age=8 * DAY)
    env.put("Notes/scratch.md", STAMP, age=2 * DAY)
    env.put("Notes/temp.md", STAMP, age=OLD)
    env.put("Notes/refers.md", "see [[temp]]")
    env.put("Notes/x.md", "the original")
    env.put("Notes/x.md.bak", "backup")
    env.put("Notes/dl.part", "half a download")
    out = vault_sweep.sweep(apply=True, only=["prune-temp"])
    got = outcomes(out, "prune-temp")
    assert got == {"Notes/tmp.md": "trashed", "Notes/scratch.md": "held", "Notes/temp.md": "linked",
                   "Notes/x.md.bak": "trashed", "Notes/dl.part": "trashed"}
    assert env.trash("Notes/tmp.md").read_text() == STAMP
    assert (env.vault / "Notes/temp.md").exists() and (env.vault / "Notes/scratch.md").exists()


def test_a_temp_name_is_trashed_whatever_it_holds(env):
    """Operator order 2026-11-10: temp and scratch files go, content or not.

    Only the whole stem counts, so a title that merely contains the word is
    never touched, and a lone .bak (maybe the only copy) is still reported.
    """
    prose = "A one-line thought jotted into a scratch file.\n" * 20
    named = ("tmp.md", "tmep.md", "scratch.md", "Temp 72.md", "project-scratch.md", "tmpab12cd34.md", "tmp.txt")
    titles = ("Learning Scratch.md", "Build a Database from Scratch.md", "Tmpanalysis.md", "temp.v2-design.md")
    for name in named + titles:
        env.put(f"Ideas/{name}", prose)
    env.put("Ideas/lonely.md.bak", prose)       # the only copy: nothing beside it
    env.put("Ideas/paired.md", "now")
    env.put("Ideas/paired.md.bak", "before")     # the original is beside it: a true backup
    env.put("Ideas/draft.md.swp", prose)         # an editor dropping: junk whatever it holds
    env.put("Ideas/blank/tmp.md", STAMP)
    out = vault_sweep.sweep(apply=True, only=["prune-temp"])
    got = outcomes(out, "prune-temp")
    assert out["errors"] == []
    trashed = sorted(p for p, o in got.items() if o == "trashed")
    assert trashed == sorted([f"Ideas/{n}" for n in named]
                             + ["Ideas/blank/tmp.md", "Ideas/draft.md.swp", "Ideas/paired.md.bak"])
    for name in named:
        assert env.trash(f"Ideas/{name}").read_text() == prose, name
    for name in titles + ("lonely.md.bak",):
        assert (env.vault / "Ideas" / name).read_text() == prose, name
    assert got["Ideas/lonely.md.bak"] == "report"
    assert not any(t in " ".join(got) for t in titles)


def test_untitled_trash_title_and_report(env, fake_pipeline):
    env.put("A/Untitled.md", "---\ncreated: 2026-01-01\npipeline-status: x\n---\n")
    env.put("B/Untitled 2.md", "Some real thoughts about Jelmore.")
    env.put("C/UNTITLED CHAT 2024-12-01 15 18 32.md", "chat body")
    env.put("D/Untitled.base", "views:\n  - type: table\n    name: Table\n")
    env.put("E/Untitled.png", data=b"\x89PNG....")
    fake_pipeline.slugs = {"UNTITLED CHAT 2024-12-01 15 18 32.md": "untitled-2"}
    out = vault_sweep.sweep(apply=True, only=["untitled"])
    got = outcomes(out, "untitled")
    assert got["A/Untitled.md"] == "trashed" and env.trash("A/Untitled.md").exists()
    assert got["B/Untitled 2.md"] == "titled_renamed"
    assert (env.vault / "B/a-great-title.md").read_text().count("Some real thoughts") == 1
    assert got["C/UNTITLED CHAT 2024-12-01 15 18 32.md"] == "unsafe_title"
    assert (env.vault / "C/UNTITLED CHAT 2024-12-01 15 18 32.md").exists()
    assert got["D/Untitled.base"] == "trashed"
    assert got["E/Untitled.png"] == "report" and (env.vault / "E/Untitled.png").exists()


def test_untitled_refuses_loudly_without_the_pipeline(env):
    env.put("B/Untitled.md", "content")
    with patch.object(pipelines, "compiled", lambda: {}):
        out = vault_sweep.sweep(only=["untitled"])
    assert out["errors"] and "not compiled" in out["errors"][0]
    assert outcomes(out, "untitled") == {"B/Untitled.md": "refused"}


def test_untitled_budget_defers(env, fake_pipeline):
    for n in range(3):
        env.put(f"N{n}/Untitled.md", f"body {n}")
    with patch.object(hygiene, "UNTITLED_MAX_PER_RUN", 2):
        out = vault_sweep.sweep(apply=True, only=["untitled"])
    assert sorted(outcomes(out, "untitled").values()) == ["deferred", "titled_renamed", "titled_renamed"]


def test_consolidate(env):
    env.put("C/Plan.md", "same\r\nbody  \n\n")
    env.put("C/Plan copy.md", "same\nbody\n")
    env.put("C/Plan copy 2.md", "different body")
    env.put("C/Resume (1).pdf", data=b"%PDF")
    env.put("C/Doc.sync-conflict-20260101-120000-ABCDEFG.pdf", data=b"%PDF-conflict")
    env.put("C/Meeting 2.md", "a meeting")
    env.put("C/Notes.md", "n")
    env.put("C/Notes 1.md", "n")
    env.put("C/Ideas (1).md", "idea")
    env.put("C/Ideas.md", "other idea", age=OLD)
    env.put("C/one.md", "dup body")
    env.put("C/two.md", "dup body")
    out = vault_sweep.sweep(apply=True, only=["consolidate"])
    got = outcomes(out, "consolidate")
    assert got["C/Plan copy.md"] == "trashed" and env.trash("C/Plan copy.md").exists()
    assert got["C/Plan copy 2.md"] == "report"
    assert got["C/Resume (1).pdf"] == "report" and (env.vault / "C/Resume (1).pdf").exists()
    assert got["C/Doc.sync-conflict-20260101-120000-ABCDEFG.pdf"] == "renamed"
    assert (env.vault / "C/Doc.pdf").read_bytes() == b"%PDF-conflict"
    assert "C/Meeting 2.md" not in got and (env.vault / "C/Meeting 2.md").exists()
    assert got["C/Notes 1.md"] == "trashed"
    assert got["C/Ideas (1).md"] == "report"
    assert got["C/one.md"] == "report" and (env.vault / "C/two.md").exists()


def test_consolidate_never_renames_or_trashes_real_titles(env):
    """Titles that merely look like copies: only a Syncthing conflict file is ever promoted to its original."""
    for name, body in (("Marketing copy.md", "headline"), ("Press release copy.md", "draft"),
                       ("Ad copy 2.md", "variant"), ("Sprint (2).md", "retro"),
                       ("Marketing Copy.md", "caps"), ("Landing Page Copy.md", "page"),
                       ("Foo - Copy.md", "dash")):
        env.put(f"Work/{name}", body)
    for n in (1, 2, 3):                      # a numbered series of identical template stubs, no "Week.md"
        env.put(f"Course/Week {n}.md", "## Goals\n\n## Notes\n")
    env.put("Work/Plan.md", "plan v1")
    env.put("Work/Plan copy.md", "plan v2 with extra")
    env.put("Work/Plan 1.md", "plan v3 with more")
    out = vault_sweep.sweep(apply=True, only=["consolidate"])
    assert out["errors"] == []
    changed = [i for i in out["actions"]["consolidate"] if i["outcome"] in vault_sweep.CHANGES]
    assert changed == [], changed
    for rel in ("Work/Marketing copy.md", "Work/Press release copy.md", "Work/Ad copy 2.md", "Work/Sprint (2).md",
                "Work/Marketing Copy.md", "Work/Landing Page Copy.md", "Work/Foo - Copy.md",
                "Course/Week 1.md", "Course/Week 2.md", "Course/Week 3.md", "Work/Plan copy.md", "Work/Plan 1.md"):
        assert (env.vault / rel).exists(), rel
    assert not (env.vault / "Work/Marketing.md").exists() and not (env.vault / "Work/Foo -.md").exists()


def test_organize_root_notes_and_empty_dirs(env):
    env.put("Loose.md", "root")
    env.put("README.md", "keep")
    env.put("TODO.md", "keep")
    env.put("Clash.md", "root")
    env.put("Inbox/Clash.md", "already there")
    for d in ("Projects/Gone/a/b", "Projects/_triage/lane", "Projects/Keep", "TopEmpty"):
        (env.vault / d).mkdir(parents=True)
    env.put("Projects/Keep/note.md", "x")
    env.age_dirs()
    out = vault_sweep.sweep(apply=True, only=["organize"])
    got = outcomes(out, "organize")
    assert got["Loose.md"] == "moved" and (env.vault / "Inbox/Loose.md").read_text() == "root"
    assert "README.md" not in got and "TODO.md" not in got
    assert got["Clash.md"] == "collision" and (env.vault / "Clash.md").exists()
    assert not (env.vault / "Projects/Gone").exists()
    assert got["Projects/Gone"] == "removed"
    assert (env.vault / "Projects/_triage/lane").is_dir() and (env.vault / "TopEmpty").is_dir()


def test_one_action_per_file(env):
    env.put("Untitled.md", "")  # zero bytes: prune-empty claims it before untitled or organize
    out = vault_sweep.sweep(apply=True)
    assert outcomes(out, "prune-empty") == {"Untitled.md": "trashed"}
    assert outcomes(out, "untitled") == {} and outcomes(out, "organize") == {}


# ---------------------------------------------------------------- exclusions --

def test_hard_exclusions(env):
    junk = "tmp.md"
    for where in ("Transcripts", "Inbox/Wax", "agents/x", ".obsidian/plugins", "_vault/templates",
                  "Code/app", "Code/app/src", "Bound", "Deps/node_modules/pkg", "Extra/zone"):
        env.put(f"{where}/{junk}", "", age=OLD)
    env.put("Code/app/package.json", "{}")
    env.put("Big/tmp.md", "x" * 64, age=OLD)
    outside = env.root / "outside"
    outside.mkdir()
    (outside / "tmp.md").write_text("")
    (env.vault / "Linked").mkdir()
    (env.vault / "Linked/tmp.md").symlink_to(outside / "tmp.md")
    (env.vault / "LinkedDir").symlink_to(outside)
    conn = ledger.connect()
    conn.execute("INSERT INTO document_bindings(binding_id,pipeline,folder,enabled,created_at,updated_at) "
                 "VALUES('b1','p',?,1,'x','x')", (str(env.vault / "Bound"),))
    with patch.dict(os.environ, {"WAX_VAULT_SWEEP_EXCLUDE": f"agents{os.pathsep}Extra/zone",
                                 "WAX_VAULT_SWEEP_MAX_BYTES": "32"}):
        out = vault_sweep.sweep(apply=True)
    moved = [i for a in out["actions"].values() for i in a if i["outcome"] in vault_sweep.CHANGES]
    assert moved == [], moved
    assert outcomes(out, "prune-temp") == {"Big/tmp.md": "held"}
    assert "Code/app" in out["excluded"]["code_projects"]
    assert {"Transcripts", "Inbox/Wax", "Bound", "agents", "Extra/zone"} <= set(out["excluded"]["zones"])
    assert (outside / "tmp.md").exists() and (env.vault / "Linked/tmp.md").is_symlink()


def test_refusal_is_one_choke_point(env):
    job = vault_sweep.Sweep(dry_run=True)
    job._build_zones()
    path = env.put("Transcripts/n.md", "")
    assert job.refusal(path, os.lstat(path), min_age=0) == "WAX_VAULT (wax transcripts prune owns it)"
    path = env.put("Notes/.hidden.md", "")
    assert job.refusal(path, os.lstat(path), min_age=0) == "dot path"
    path = env.put("Notes/new.md", "", age=10)
    assert job.refusal(path, os.lstat(path), min_age=DAY) == "young"


def test_vault_root_refusals(env):
    with patch.object(paths, "VAULT", env.root / "nowhere"):
        with pytest.raises(vault_sweep.VaultSweepError, match="not inside an Obsidian vault"):
            vault_sweep.sweep()
    with patch.object(paths, "VAULT", env.vault):
        with pytest.raises(vault_sweep.VaultSweepError, match="vault root itself"):
            vault_sweep.sweep()
    with pytest.raises(vault_sweep.VaultSweepError, match="unknown action"):
        vault_sweep.sweep(only=["nuke"])


# ------------------------------------------------------- journal and safety --

def test_manifest_entry_is_written_before_each_move(env):
    env.put("Notes/tmp.md", STAMP)
    env.put("Loose.md", "root")
    real = rename.renameat2_noreplace
    seen = []

    def checking(src, dst):
        tos = [e.get("to") for e in vault_sweep.read_manifest()]
        assert str(dst) in tos, f"{dst} moved before it was journaled"
        seen.append(dst)
        return real(src, dst)

    with patch.object(rename, "renameat2_noreplace", checking):
        out = vault_sweep.sweep(apply=True, only=["prune-temp", "organize"])
    assert len(seen) == 2 and out["errors"] == []
    entry = next(e for e in vault_sweep.read_manifest() if e["from"].endswith("tmp.md"))
    assert entry["op"] == "trash" and entry["sha256"] and entry["ino"] and entry["run"] == out["run"]


def test_a_file_changed_after_planning_is_left_alone(env):
    path = env.put("Notes/draft.md.swp", "an editor's leftovers")
    job = vault_sweep.Sweep(dry_run=False)
    job.run = "test-run"
    job.walk()
    path.write_text("somebody typed here")
    os.utime(path, (time.time() - OLD, time.time() - OLD + 5))
    job.act_prune_temp()
    assert outcomes({"actions": job.results}, "prune-temp") == {"Notes/draft.md.swp": "changed"}
    assert path.exists()


def test_a_locked_document_is_skipped_and_ledger_follows_moves(env):
    locked = env.put("Notes/tmp.md", STAMP)
    free = env.put("Notes/temp.md", STAMP.replace("new", "two"))
    row_locked, _ = documents.register(locked, binding_id="", origin="test")
    row_free, _ = documents.register(free, binding_id="", origin="test")
    lock = documents.document_run_lock("", row_locked["doc_id"])
    assert lock.acquire()
    try:
        out = vault_sweep.sweep(apply=True, only=["prune-temp"])
    finally:
        lock.release()
    assert outcomes(out, "prune-temp") == {"Notes/tmp.md": "locked", "Notes/temp.md": "trashed"}
    state = ledger.connect().execute("SELECT state FROM documents WHERE doc_id=?", (row_free["doc_id"],)).fetchone()
    assert state["state"] == "trashed"
    back = vault_sweep.restore(run=out["run"])
    assert back["errors"] == [] and len(back["restored"]) == 1
    row = ledger.connect().execute("SELECT state, path FROM documents WHERE doc_id=?",
                                   (row_free["doc_id"],)).fetchone()
    assert row["state"] == row_free["state"] and row["path"] == str(free)


def test_trash_never_clobbers(env):
    env.put("Notes/tmp.md", STAMP)
    env.put(".trash/Notes/tmp.md", "older trash")
    out = vault_sweep.sweep(apply=True, only=["prune-temp"])
    assert env.trash("Notes/tmp.md").read_text() == "older trash"
    assert env.trash("Notes/tmp-1.md").read_text() == STAMP
    assert out["actions"]["prune-temp"][0]["to"] == ".trash/Notes/tmp-1.md"


def test_limit_caps_changes(env):
    for n in range(4):
        env.put(f"Notes/tmp{n}.md", STAMP)
    out = vault_sweep.sweep(apply=True, only=["prune-temp"], limit=2)
    assert out["changes"] == 2 and out["limit_reached"]
    assert len(list((env.vault / "Notes").glob("tmp*.md"))) == 2


def test_apply_refuses_while_another_run_holds_the_lock(env):
    import fcntl
    with open(env.var / vault_sweep.LOCK_NAME, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(vault_sweep.VaultSweepError, match="holds"):
            vault_sweep.sweep(apply=True)
        vault_sweep.sweep()  # a dry run needs no lock


def test_unsweep_puts_a_whole_run_back(env, fake_pipeline):
    env.put("Notes/empty.md", "")
    env.put("Notes/tmp.md", STAMP)
    env.put("Notes/Untitled.md", "real words")
    env.put("Notes/Plan.md", "same")
    env.put("Notes/Plan copy.md", "same")
    env.put("Notes/Resume.sync-conflict-20260101-120000-ABCDEFG.pdf", data=b"%PDF")
    env.put("Loose.md", "root")
    (env.vault / "Notes/Empty/deeper").mkdir(parents=True)
    env.age_dirs()
    out = vault_sweep.sweep(apply=True)
    assert out["errors"] == [] and out["changes"] == 8 and ledger.meta_get(vault_sweep.META_LAST_RUN)
    assert not (env.vault / "Notes/Empty").exists()
    dry = vault_sweep.restore(run=out["run"], dry_run=True)
    assert len(dry["restored"]) == 9 and not (env.vault / "Notes/Empty").exists()  # 8 changes + the untitled rewrite
    back = vault_sweep.restore(run=out["run"])
    assert back["errors"] == [] and back["skipped"] == [] and len(back["restored"]) == 9
    for rel in ("Notes/empty.md", "Notes/tmp.md", "Notes/Untitled.md", "Notes/Plan copy.md",
                "Notes/Resume.sync-conflict-20260101-120000-ABCDEFG.pdf", "Loose.md"):
        assert (env.vault / rel).exists(), rel
    assert (env.vault / "Notes/Untitled.md").read_text() == "real words"  # the pre-image went back too
    assert (env.vault / "Notes/Empty/deeper").is_dir()
    assert not (env.vault / "Inbox/Loose.md").exists() and not (env.vault / "Notes/a-great-title.md").exists()
    again = vault_sweep.restore(run=out["run"])
    assert again["restored"] == [] and again["skipped"]
    with pytest.raises(vault_sweep.VaultSweepError, match="no vault-sweep run"):
        vault_sweep.restore(run="19990101T000000Z")


def test_unsweep_will_not_move_a_different_file(env):
    env.put("Notes/tmp.md", STAMP)
    out = vault_sweep.sweep(apply=True, only=["prune-temp"])
    trashed = env.trash("Notes/tmp.md")
    trashed.rename(env.root / "kept-aside.md")  # keep the inode alive so the impostor cannot reuse it
    trashed.write_text("an impostor")
    back = vault_sweep.restore(run=out["run"])
    assert back["restored"] == [] and "no longer the file" in back["skipped"][0]["detail"]
    assert not (env.vault / "Notes/tmp.md").exists()


# ---------------------------------------------------- review fixes: regressions --

def test_prune_empty_keeps_the_authors_metadata_and_linked_stubs(env):
    """aliases/tags/date/id are things somebody wrote and other notes link to; only blanks and stamps are empty."""
    env.put("People/Bob.md", "---\naliases: [Robert, Bobby]\ntags: [person]\n---\n")
    env.put("Tags/project.md", "---\ntags: [project]\n---\n")
    env.put("Daily/2025-01-01.md", "---\ndate: 2025-01-01\nid: abc123\n---\n")
    env.put("Notes/Ref.md", "See [[Robert]] and [[Bobby]] and [[Linked Stub]]\n")
    env.put("Notes/Linked Stub.md", "")                       # zero bytes, but a note links to it
    env.put("Notes/Blank.md", "---\ntags: []\naliases: ['']\ncreated: 2026-01-01\n---\n\n")
    env.put("Notes/Zero.md", "")
    out = vault_sweep.sweep(apply=True, only=["prune-empty"])
    got = outcomes(out, "prune-empty")
    assert got == {"Notes/Linked Stub.md": "linked", "Notes/Blank.md": "trashed", "Notes/Zero.md": "trashed"}
    for rel in ("People/Bob.md", "Tags/project.md", "Daily/2025-01-01.md", "Notes/Linked Stub.md"):
        assert (env.vault / rel).exists(), rel
    assert not vault_sweep.strictly_empty("---\naliases: [x]\n---\n")
    assert vault_sweep.strictly_empty("---\ncreated: 2026-01-01\nupdated: 2026-01-02\n---\n")
    assert not vault_sweep.stamp_only("---\ntags: [x]\n---\n") and not vault_sweep.stamp_only("---\ndate: 2025-01-01\n---\n")


def test_a_package_marker_is_not_an_empty_file(env):
    env.put("Scripts/tool/__init__.py", data=b"")
    env.put("Scripts/tool/py.typed", data=b"")
    env.put("Plain/py.typed", data=b"")
    env.put("Plain/zero.dat", data=b"")
    out = vault_sweep.sweep(apply=True, only=["prune-empty"])
    assert outcomes(out, "prune-empty") == {"Plain/zero.dat": "trashed"}   # Scripts/tool is a code project
    assert "Scripts/tool" in out["excluded"]["code_projects"]
    for rel in ("Scripts/tool/__init__.py", "Scripts/tool/py.typed", "Plain/py.typed"):
        assert (env.vault / rel).exists(), rel


def test_one_bad_file_or_failing_step_never_stops_the_run(env):
    (env.vault / "Misc").mkdir()
    bad = os.fsencode(str(env.vault / "Misc")) + b"/caf\xe9.md.bak"
    with open(bad, "wb") as fh:
        fh.write(b"real")
    old = time.time() - OLD
    os.utime(bad, (old, old))
    env.put("Aaa/tmp.md", STAMP)
    env.put("Zzz/tmp.md", STAMP)
    out = vault_sweep.sweep(apply=True, only=["prune-temp"])
    json.dumps(out)                                           # printable and serialisable
    assert out["errors"] == [] and outcomes(out, "prune-temp") == {"Aaa/tmp.md": "trashed", "Zzz/tmp.md": "trashed"}
    assert len(out["skipped"]) == 1 and "udce9" in out["skipped"][0]["path"] and os.path.exists(bad)

    # the ledger failing AFTER a move: the file is in the trash and journaled, the error says so, the run goes on
    env2 = [env.put(f"Led/tmp{n}.md", STAMP.replace("new", f"n{n}")) for n in range(2)]  # distinct bytes, distinct rows
    for path in env2:
        documents.register(path, binding_id="", origin="test")
    import sqlite3
    with patch.object(documents, "mark_state", side_effect=sqlite3.OperationalError("database is locked")):
        out = vault_sweep.sweep(apply=True, only=["prune-temp"])
    assert outcomes(out, "prune-temp") == {"Led/tmp0.md": "trashed", "Led/tmp1.md": "trashed"}
    assert len(out["errors"]) == 2 and "ledger was not updated" in out["errors"][0]
    assert out["run"] and ledger.meta_get(vault_sweep.META_LAST_RUN)

    # an action blowing up costs that action only
    env.put("Loose.md", "root note")
    with patch.object(vault_sweep.Sweep, "act_prune_temp", side_effect=RuntimeError("boom")):
        out = vault_sweep.sweep(apply=True)
    assert any("prune-temp: RuntimeError: boom" in e for e in out["errors"])
    assert outcomes(out, "organize") == {"Loose.md": "moved"} and out["run"]


def test_a_file_put_back_is_kept(env):
    env.put("Work/scratch.md", STAMP)
    env.put("Work/draft.md.swp", "an editor's leftovers")
    night1 = vault_sweep.sweep(apply=True)
    assert sorted(outcomes(night1, "prune-temp").values()) == ["trashed", "trashed"]
    assert vault_sweep.restore(run=night1["run"])["errors"] == []
    night2 = vault_sweep.sweep(apply=True)
    assert sorted(outcomes(night2, "prune-temp").values()) == ["held", "held"]
    assert "put back" in night2["actions"]["prune-temp"][0]["detail"]
    assert (env.vault / "Work/scratch.md").exists() and (env.vault / "Work/draft.md.swp").exists()
    # rescued from the trash by hand, no unsweep involved
    env.put("Work/tmp.md", STAMP)
    night3 = vault_sweep.sweep(apply=True)
    assert outcomes(night3, "prune-temp")["Work/tmp.md"] == "trashed"
    env.trash("Work/tmp.md").rename(env.vault / "Work/tmp.md")
    night4 = vault_sweep.sweep(apply=True)
    assert outcomes(night4, "prune-temp")["Work/tmp.md"] == "held"


def test_ctime_counts_as_age(env, monkeypatch):
    """Syncthing (or a hand-rescue) hands over a file with an old mtime and a new ctime: it is new."""
    monkeypatch.setattr(vault_sweep, "_touched", REAL_TOUCHED)
    env.put("Notes/draft.md.swp", "x")                        # mtime 30 days old, ctime now
    out = vault_sweep.sweep(only=["prune-temp"])
    assert outcomes(out, "prune-temp") == {"Notes/draft.md.swp": "held"}
    assert "young" in out["actions"]["prune-temp"][0]["detail"]
    later = vault_sweep.sweep(only=["prune-temp"], now=time.time() + 8 * DAY)
    assert outcomes(later, "prune-temp") == {"Notes/draft.md.swp": "would_trash"}


def test_untitled_looks_again_before_running_the_pipeline(env, fake_pipeline):
    quiet = env.put("Notes/Untitled.md", "old thought\n")
    env.put("Notes/Untitled 2.md", "another old thought\n")
    job = vault_sweep.Sweep(dry_run=False, only=["untitled"])
    job.run = "r1"
    job.walk()
    # the user is typing in it right now, after the plan was made
    quiet.write_text("old thought\nTYPING RIGHT NOW\n")
    job.act_untitled()
    got = {i["path"]: i["outcome"] for i in job.results["untitled"]}
    assert got["Notes/Untitled.md"] == "changed" and got["Notes/Untitled 2.md"] == "titled_renamed"
    assert [c.name for c in fake_pipeline.calls] == ["Untitled 2.md"]
    assert quiet.read_text().endswith("TYPING RIGHT NOW\n")


def test_untitled_keeps_a_preimage_and_unsweep_puts_the_note_back_byte_for_byte(env, fake_pipeline):
    original = "---\ntags: [a]  # keep this comment\n---\nreal words\n"
    path = env.put("Notes/Untitled.md", original)
    mtime = os.lstat(path).st_mtime
    seen = []
    real = pipelines.run_document

    def checking(name, p, **kw):
        kept = list((env.vault / ".trash" / vault_sweep.PREIMAGE_DIR).rglob("Untitled.md"))
        seen.append([k.read_text() for k in kept])
        return real(name, p, **kw)

    with patch.object(pipelines, "run_document", checking):
        out = vault_sweep.sweep(apply=True, only=["untitled"])
    assert seen == [[original]], "the pre-image must exist before the pipeline rewrites the note"
    titled = env.vault / "Notes/a-great-title.md"
    assert titled.exists() and "summary" in titled.read_text() and not path.exists()
    back = vault_sweep.restore(run=out["run"])
    assert back["errors"] == [] and back["skipped"] == []
    assert path.read_text() == original and not titled.exists()
    assert abs(os.lstat(path).st_mtime - mtime) < 2
    again = vault_sweep.sweep(apply=True, only=["untitled"])           # and it is not titled all over again
    assert outcomes(again, "untitled") == {"Notes/Untitled.md": "held"} and len(fake_pipeline.calls) == 1


def test_unsweep_never_overwrites_a_note_edited_since(env, fake_pipeline):
    env.put("Notes/Untitled.md", "real words\n")
    out = vault_sweep.sweep(apply=True, only=["untitled"])
    titled = env.vault / "Notes/a-great-title.md"
    titled.write_text(titled.read_text() + "\nMY NEW PARAGRAPH\n")
    back = vault_sweep.restore(run=out["run"])
    assert len(back["restored"]) == 1 and back["restored"][0]["op"] == "rename"
    assert len(back["skipped"]) == 1 and "edited since the sweep" in back["skipped"][0]["detail"]
    assert "MY NEW PARAGRAPH" in (env.vault / "Notes/Untitled.md").read_text()
    assert list((env.vault / ".trash" / vault_sweep.PREIMAGE_DIR).rglob("Untitled.md"))


def test_the_link_guard_sees_every_way_a_note_gets_referenced(env):
    for n in (11, 19, 22, 23, 25, 26, 27, 28, 29, 30):
        env.put(f"Rollups/tmp{n}.md", STAMP)
    env.put("Linkers/table.md", "| col |\n|---|\n| [[tmp11\\|my alias]] |\n")
    env.put("Linkers/html.md", '<a href="tmp19.md">x</a>\n')
    env.put("Linkers/latin1.md", data=b"caf\xe9 [[tmp22]]\n")
    env.put("Linkers/base.base", 'filters:\n  and:\n    - file.name == "tmp25"\n')
    env.put(".obsidian/plugins/quickadd/data.json",
            '{"captureTo": "Rollups/tmp26.md", "elsewhere": "Inbox/tmp29.md", "label": "tmp28", "bare": "tmp30.md"}')
    env.put(".obsidian/workspace.json", '{"lastOpenFile": "tmp27.md"}')    # UI state, not a reference
    env.put(".obsidian/workspace-mobile.json", '{"lastOpenFile": "Rollups/tmp27.md"}')
    env.put("Linkers/split.md", "x" * 60 + "[[tmp23]]\n")                  # a link across a chunk boundary
    with patch.object(vault_sweep, "LINK_SCAN_CHUNK", 64):
        out = vault_sweep.sweep(only=["prune-temp"])
    got = {Path(p).stem: o for p, o in outcomes(out, "prune-temp").items()}
    linked = ("tmp11", "tmp19", "tmp22", "tmp23", "tmp25", "tmp26", "tmp30")
    assert {n: got[n] for n in linked} == dict.fromkeys(linked, "linked")
    # UI state, a bare word and a path to a DIFFERENT folder are not references
    assert got["tmp27"] == "would_trash" and got["tmp28"] == "would_trash" and got["tmp29"] == "would_trash"
    big = env.put("Linkers/huge.md", "[[tmp28]]\n" + "y" * 200)
    with patch.object(vault_sweep, "LINK_SCAN_CHUNK", 32):                   # no size cap on a linker
        assert outcomes(vault_sweep.sweep(only=["prune-temp"]), "prune-temp")["Rollups/tmp28.md"] == "linked"
    assert big.exists()


def test_open_history_is_not_a_link_and_bookmarks_are_exact_paths(env):
    for rel in ("Inbox/tmp.md", "Rollups/scratch.md", "Deep/x/scratch.md", "Kept/tmp.md"):
        env.put(rel, "real words\n")
    # recent-files is what you opened, not what you keep
    env.put(".obsidian/plugins/recent-files-obsidian/data.json",
            '{"recentFiles": [{"basename": "tmp", "path": "Inbox/tmp.md"}, {"path": "tmp.md"}]}')
    # a bookmark path is vault-relative: "scratch.md" is the (missing) root file only
    env.put(".obsidian/bookmarks.json",
            '{"items": [{"type": "file", "path": "scratch.md"}, {"type": "file", "path": "Kept/tmp.md"}]}')
    got = outcomes(vault_sweep.sweep(only=["prune-temp"]), "prune-temp")
    assert got["Inbox/tmp.md"] == "would_trash"
    assert got["Rollups/scratch.md"] == "would_trash" and got["Deep/x/scratch.md"] == "would_trash"
    assert got["Kept/tmp.md"] == "linked"


DAILY = STAMP + """## Morning Checklist
- [ ] Check [calendar](https://calendar.google.com)
- [ ] Create today's prioritized task list

## Top 3 Priorities
1. <% tp.file.cursor(3) %>
2. <% tp.file.cursor(4) %>

## Notes
<% tp.file.cursor(6) %>

## Habits
- [ ] Drink 8 glasses of water
- [ ]
"""


def test_unfilled_template_stubs_are_trashed_and_filled_ones_kept(env):
    for day in ("01", "02", "03"):
        env.put(f"Dailies/2025-01-{day}.md", DAILY)
    env.put("Dailies/2025-01-04.md", DAILY.replace("## Notes\n", "## Notes\n\n\n"))       # blank lines only
    env.put("Dailies/2025-01-05.md", DAILY.replace("<% tp.file.cursor(6) %>", "<% tp.file.cursor(6) %>\nIan ma"))
    env.put("Dailies/2025-01-06.md", DAILY.replace("- [ ] Check [cal", "- [x] Check [cal"))  # ticked
    env.put("Dailies/2025-01-07.md", DAILY.replace("- [/] ", "").replace("- [ ] Create", "- [/] Create"))
    env.put("Dailies/2025-01-08.md", DAILY.replace("- [ ] Drink", "- [ ] Call mom\n- [ ] Drink"))  # typed task
    env.put("Dailies/2025-01-09.md", DAILY.replace("pipeline-status: new", "tags: [work]"))   # author's key
    env.put("Dailies/2025-01-10.md", DAILY.replace("<%", "[").replace("%>", "]"))            # no template tag
    got = outcomes(vault_sweep.sweep(only=["prune-empty"]), "prune-empty")
    trashed = {f"Dailies/2025-01-{d}.md" for d in ("01", "02", "03", "04")}
    assert {p for p, o in got.items() if o == "would_trash"} == trashed
    out = vault_sweep.sweep(apply=True, only=["prune-empty"])
    assert {p for p, o in outcomes(out, "prune-empty").items() if o == "trashed"} == trashed
    assert (env.vault / ".trash/Dailies/2025-01-01.md").read_text() == DAILY
    for d in ("05", "06", "07", "08", "09", "10"):
        assert (env.vault / f"Dailies/2025-01-{d}.md").exists()


def test_names_that_differ_only_by_case_collide(env, fake_pipeline):
    env.put("Work/Budget.md", "budget v1")
    env.put("Work/budget.sync-conflict-20260101-120000-ABCDEFG.md", "budget v2")
    env.put("Inbox/Idea.md", "inbox idea")
    env.put("idea.md", "root idea")
    env.put("Work2/Budget.md", "other budget")
    env.put("Work2/Untitled.md", "words")
    fake_pipeline.slugs = {"Untitled.md": "budget"}
    out = vault_sweep.sweep(apply=True, only=["consolidate", "organize", "untitled"])
    assert outcomes(out, "consolidate")["Work/budget.sync-conflict-20260101-120000-ABCDEFG.md"] == "collision"
    assert outcomes(out, "organize")["idea.md"] == "collision"
    assert outcomes(out, "untitled")["Work2/Untitled.md"] == "collision"
    assert sorted(os.listdir(env.vault / "Inbox")) == ["Idea.md"] and (env.vault / "idea.md").exists()
    assert (env.vault / "Work2/Untitled.md").exists() and sorted(
        n for n in os.listdir(env.vault / "Work2")) == ["Budget.md", "Untitled.md"]


def test_a_limit_below_one_is_refused_not_unlimited(env):
    for bad in (0, -1):
        with pytest.raises(vault_sweep.VaultSweepError, match="at least 1"):
            vault_sweep.sweep(limit=bad)
    assert vault_sweep.sweep(limit=1)["limit"] == 1


def test_the_dry_run_is_the_action(env):
    """What the dry run lists is exactly what --apply does, including a survivor trashed earlier in the run."""
    env.put("Notes/tmp.md", STAMP)
    env.put("Notes/tmp copy.md", "same words", age=OLD - 10)
    env.put("Notes/tmp copy 2.md", "same words", age=OLD - 20)
    env.put("Notes/Gone.md", "")
    env.put("Notes/Gone.sync-conflict-20260101-120000-ABCDEFG.md", "the only copy")
    env.put("Loose.md", "root")
    (env.vault / "Notes/Hollow").mkdir()
    env.age_dirs()
    final = {"would_trash": "trashed", "would_rename": "renamed", "would_move": "moved", "would_remove": "removed"}

    def plan(summary):
        return sorted((a, i["path"], final.get(i["outcome"], i["outcome"]), i.get("detail", ""))
                      for a in summary["only"] for i in summary["actions"][a]
                      if i["outcome"] not in ("held", "locked"))

    dry = vault_sweep.sweep()
    done = vault_sweep.sweep(apply=True)
    assert dry["errors"] == [] and done["errors"] == []
    assert plan(dry) == plan(done)
    assert not any("differs" in row[3] for row in plan(done))
    assert (env.vault / "Notes/Gone.md").read_text() == "the only copy"


def test_doctor_reports_the_last_apply_run(env):
    import importlib.util
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("wax_cli_vault_sweep", str(COMPONENT / "bin" / "wax"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    cli = importlib.util.module_from_spec(spec)
    loader.exec_module(cli)

    def probe() -> dict:
        rows: list = []
        cli.probe_vault_sweep(rows.append)
        assert len(rows) == 1 and rows[0]["probe"] == "vault sweep ran"
        return rows[0]

    assert probe()["status"] == cli.PASS and "no apply run yet" in probe()["detail"]
    env.put("Notes/tmp.md", STAMP)
    out = vault_sweep.sweep(apply=True)
    row = probe()
    assert row["status"] == cli.PASS and out["run"] in row["detail"] and "1 change(s), 0 error(s)" in row["detail"]
    ledger.meta_set(vault_sweep.META_LAST_RUN, json.dumps({"run": "r", "at": "2026-01-01T00:00:00Z", "changes": 3,
                                                           "errors": 0}))
    assert probe()["status"] == cli.WARN                                   # overdue: the nightly job stopped
    ledger.meta_set(vault_sweep.META_LAST_RUN, json.dumps({"run": "r", "at": sentinel.utcnow(),
                                                           "changes": 3, "errors": 2}))
    assert probe()["status"] == cli.WARN and "2 error(s)" in probe()["detail"]


# ------------------------------------------------------------------------ CLI --

def _cli(env: Env, *argv: str, vault: Path = None) -> subprocess.CompletedProcess:
    # the subprocess sees real ctimes (a test cannot backdate them): a negative age floor makes every file old
    run_env = {**os.environ, "WAX_ROOT": str(env.runtime), "WAX_VAULT": str(vault or env.vault / "Transcripts"),
               "TRANSCRIPTS": str(vault or env.vault / "Transcripts"),
               "WAX_VAULT_SWEEP_MIN_AGE_S": "-1", "WAX_VAULT_SWEEP_TEMP_MIN_AGE_S": "-1"}
    return subprocess.run([sys.executable, str(COMPONENT / "bin" / "wax"), *argv], capture_output=True,
                          text=True, env=run_env, timeout=120)


def test_cli_dry_run_apply_and_unsweep(env):
    env.put("Notes/tmp.md", STAMP)
    dry = _cli(env, "vault", "sweep", "--json")
    assert dry.returncode == 0, dry.stderr
    assert json.loads(dry.stdout)["counts"]["prune-temp"] == {"would_trash": 1}
    assert (env.vault / "Notes/tmp.md").exists()
    human = _cli(env, "vault", "sweep")
    assert human.returncode == 0 and "WOULD_TRASH" in human.stdout and "dry run: nothing changed" in human.stdout
    applied = _cli(env, "--json", "vault", "sweep", "--apply", "--only", "prune-temp")
    assert applied.returncode == 0, applied.stderr
    run = json.loads(applied.stdout)["run"]
    assert env.trash("Notes/tmp.md").exists()
    undo = _cli(env, "vault", "unsweep", "--run", run)
    assert undo.returncode == 0, undo.stderr
    assert (env.vault / "Notes/tmp.md").exists()
    assert _cli(env, "vault", "unsweep", "--run", run).returncode == 2  # nothing left to restore is not success


def test_cli_refusals_exit_2(env):
    outside = env.root / "plain"
    outside.mkdir()
    refused = _cli(env, "vault", "sweep", vault=outside)
    assert refused.returncode == 2 and "not inside an Obsidian vault" in refused.stderr
    assert _cli(env, "vault", "unsweep", "--run", "nope").returncode == 2
    assert _cli(env, "vault", "sweep", "--only", "nuke").returncode == 2


def test_cli_limit_zero_is_refused_and_a_bad_name_does_not_crash_it(env):
    env.put("Notes/tmp.md", STAMP)
    for flag in ("--limit=0", "--limit=-1"):
        refused = _cli(env, "vault", "sweep", flag)
        assert refused.returncode == 2 and "at least 1" in refused.stderr, refused
    bad = os.fsencode(str(env.vault / "Notes")) + b"/caf\xe9.md"
    with open(bad, "wb") as fh:
        fh.write(b"x")
    for argv in (("vault", "sweep"), ("--json", "vault", "sweep", "--apply")):
        ran = _cli(env, *argv)
        assert ran.returncode == 0, (argv, ran.stderr[-300:])
    human = _cli(env, "vault", "sweep")
    assert "SKIPPED" in human.stdout and "UTF-8" in human.stdout


def test_a_full_applied_run_also_runs_the_inbox_hygiene_sweeps(env):
    """waxd's own hygiene clock is gone (2026-11-10): the nightly full run is what sweeps Inbox/Wax."""
    wax_doc = env.put("Inbox/Wax/note.md", "---\ncreated: 2026-01-01\n---\n")
    env.put("Notes/real.md", "words")
    folders = os.pathsep.join([str(env.vault / "Inbox"), str(env.root / "elsewhere" / "Inbox")])
    (env.root / "elsewhere" / ".obsidian").mkdir(parents=True)
    (env.root / "elsewhere" / "Inbox").mkdir()
    other = env.root / "elsewhere" / "Inbox" / "empty.md"
    other.write_text("")
    os.utime(other, (time.time() - OLD, time.time() - OLD))
    with patch.object(paths, "AUDIO", env.runtime), \
            patch.dict(os.environ, {"WAX_DOC_HYGIENE_FOLDERS": folders}), \
            patch.object(pipelines, "compiled", return_value={hygiene.UNTITLED_PIPELINE: {}}):
        dry = vault_sweep.sweep()
        assert "inbox_hygiene" not in dry and wax_doc.exists(), "a dry run never runs it"
        partial = vault_sweep.sweep(apply=True, only=["prune-empty"])
        assert "inbox_hygiene" not in partial and wax_doc.exists(), "nor does an --only run"
        out = vault_sweep.sweep(apply=True)
    hy = out["inbox_hygiene"]
    assert hy["folders"] == [str(env.vault / "Inbox")], "a folder outside the swept vault is left alone"
    assert hy["empty"]["counts"].get("trashed") == 1 and hy["empty"]["error"] is None
    assert not wax_doc.exists() and env.trash("Inbox/Wax/note.md").is_file()
    assert other.exists()
    assert hygiene.load_summary("empty")["counts"]["trashed"] == 1, "recorded for the doctor probe"
    assert out["errors"] == []


def test_inbox_hygiene_never_runs_against_a_foreign_ledger(env):
    wax_doc = env.put("Inbox/Wax/note.md", "---\ncreated: 2026-01-01\n---\n")
    with patch.dict(os.environ, {"WAX_DOC_HYGIENE_FOLDERS": str(env.vault / "Inbox")}):
        out = vault_sweep.sweep(apply=True)     # paths.AUDIO is the real root, the ledger a tmp one
    assert "runtime root" in out["inbox_hygiene"]["skipped"]
    assert wax_doc.exists()
