"""Project resolution and local calendar boundaries for wax tx (no production DSN)."""

import datetime as dt
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from wax.transcriptions import tx

UTC = dt.timezone.utc


@pytest.mark.parametrize("instant,window,start,end", [
    ("2026-03-08T16:00:00+00:00", "today", "2026-03-08T05:00:00+00:00", "2026-03-08T16:00:00+00:00"),
    ("2026-03-09T16:00:00+00:00", "yesterday", "2026-03-08T05:00:00+00:00", "2026-03-09T04:00:00+00:00"),
    ("2026-11-01T16:00:00+00:00", "today", "2026-11-01T04:00:00+00:00", "2026-11-01T16:00:00+00:00"),
    ("2026-11-02T16:00:00+00:00", "yesterday", "2026-11-01T04:00:00+00:00", "2026-11-02T05:00:00+00:00"),
    ("2026-11-04T16:00:00+00:00", "week", "2026-11-02T05:00:00+00:00", "2026-11-04T16:00:00+00:00"),
])
def test_calendar_dst(instant, window, start, end):
    with patch.object(tx.config, "local_tz", return_value=ZoneInfo("America/New_York")):
        assert tx.bounds(window, dt.datetime.fromisoformat(instant)) == (
            dt.datetime.fromisoformat(start), dt.datetime.fromisoformat(end))


def test_metadata_id_resolution_without_registry():
    projects = [{"project_id": "transcription-queue", "name": "HeyMa"}]
    with patch.object(tx.pjregistry, "fetch_registry", side_effect=AssertionError("registry called")):
        assert tx._resolve("Transcription-Queue", projects) == "transcription-queue"


def test_canonical_registry_id_wins_over_stored_display_name():
    projects = [{"project_id": "pilot", "name": "Wax"}]
    raw = {"projects": {"wax": {"project_id": "wax", "name": "Another project"}}}
    with patch.object(tx.pjregistry, "fetch_registry", return_value=raw):
        assert tx._resolve("wax", projects) == "wax"


def test_display_name_and_registry_alias_collision_is_ambiguous():
    projects = [{"project_id": "pilot", "name": "Wax"}]
    raw = {"projects": {"other": {"project_id": "other", "name": "Other"}}}
    with patch.object(tx.pjregistry, "fetch_registry", return_value=raw), \
         patch.object(tx.pjregistry, "load_aliases", return_value={"other": {"aliases": ["wax"]}}):
        with pytest.raises(tx.TxError, match="ambiguous project.*other, pilot"):
            tx._resolve("wax", projects)


def test_alias_and_canonical_id_priority():
    raw = {"projects": {"pilot": {"project_id": "pilot", "name": "Pilot"},
                        "wax": {"project_id": "wax", "name": "Other"}}}
    with patch.object(tx.pjregistry, "fetch_registry", return_value=raw), \
         patch.object(tx.pjregistry, "load_aliases", return_value={"pilot": {"aliases": ["wax", "flight"]}}):
        assert tx._resolve("wax", []) == "wax"
        assert tx._resolve("flight", []) == "pilot"


def test_ambiguous_names_and_aliases():
    projects = [{"project_id": "alpha", "name": "Shared"}, {"project_id": "beta", "name": "Shared"}]
    raw = {"projects": {pid: {"project_id": pid, "name": pid} for pid in ("alpha", "beta")}}
    aliases = {pid: {"aliases": ["common"]} for pid in ("alpha", "beta")}
    with patch.object(tx.pjregistry, "fetch_registry", return_value=raw), \
         patch.object(tx.pjregistry, "load_aliases", return_value=aliases):
        with pytest.raises(tx.TxError, match="ambiguous project.*alpha, beta"):
            tx._resolve("shared", projects)
        with pytest.raises(tx.TxError, match="ambiguous project.*alpha, beta"):
            tx._resolve("common", [])


def test_project_queries_only_metadata_ids():
    row = {"transcript_id": 15, "effective_at": "2026-11-04T12:00:00Z"}
    calls = []

    def browse(**kwargs):
        calls.append(kwargs)
        return {"rows": [row], "projects": [{"project_id": "transcription-queue", "name": "HeyMa"}],
                "truncated": False}

    with patch.object(tx.browse, "run", side_effect=browse), \
         patch.object(tx.search, "show", return_value={"version": {"version_no": 1}, "content": "body"}), \
         patch.object(tx.pjregistry, "fetch_registry", return_value={"projects": {}}), \
         patch.object(tx.pjregistry, "load_aliases", return_value=None):
        out = tx.run(project="HeyMa", include_hermes=True)
    assert out["project"] == "transcription-queue"
    assert calls[1]["project"] == "transcription-queue"
    assert calls[1]["include_hermes"] is True


def test_unknown_and_registry_error():
    raw = {"projects": {"pilot": {"project_id": "pilot", "name": "Pilot"}}}
    with patch.object(tx.pjregistry, "fetch_registry", return_value=raw), \
         patch.object(tx.pjregistry, "load_aliases", return_value=None):
        with pytest.raises(tx.TxError, match="unknown project.*pilot"):
            tx._resolve("nonexistent", [])
    with patch.object(tx.pjregistry, "fetch_registry", side_effect=tx.pjregistry.RegistryError("registry_unavailable", "offline")):
        with pytest.raises(tx.TxError, match="registry_unavailable"):
            tx._resolve("nonexistent", [])


def test_run_latest_and_listing():
    row = {"transcript_id": 15, "effective_at": "2026-11-04T12:00:00Z", "title": "Thing", "projects": []}
    calls = []

    def browse(**kwargs):
        calls.append(kwargs)
        return {"rows": [row], "projects": [], "truncated": False}

    now = dt.datetime(2026, 11, 4, 16, tzinfo=UTC)
    with patch.object(tx.browse, "run", side_effect=browse), \
         patch.object(tx.search, "show", return_value={"version": {"version_no": 1}, "content": "body"}) as show:
        result = tx.run(now=now)
        assert result["mode"] == "latest" and result["transcript"]["content"] == "body"
        assert calls[0]["until"] == now and calls[0]["limit"] == 1
        show.assert_called_once_with("#15", raw=False)
        assert tx.run(window="today", now=now)["mode"] == "list"
        show.assert_called_once()


def test_empty_latest_and_empty_listing():
    with patch.object(tx.browse, "run", return_value={"rows": [], "projects": [], "truncated": False}):
        with pytest.raises(tx.TxError, match="no transcript found"):
            tx.run()
        assert tx.run(listing=True)["rows"] == []


def test_truncation_applies_only_to_lists():
    result = {"rows": [{"transcript_id": 15}], "projects": [], "truncated": True}
    with patch.object(tx.browse, "run", return_value=result), \
         patch.object(tx.search, "show", return_value={"version": {"version_no": 1}, "content": "body"}):
        assert tx.run()["truncated"] is False
        assert tx.run(listing=True)["truncated"] is True


def test_limit_and_raw_validation():
    with pytest.raises(tx.TxError, match="--limit"):
        tx.run(limit=tx.browse.MAX_LIMIT + 1)
    with pytest.raises(tx.TxError, match="--raw"):
        tx.run(raw=True, listing=True)
