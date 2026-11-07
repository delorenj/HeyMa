"""wax tx parser and output contract, with store operations stubbed in-process."""

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_CLI_PATH = Path(__file__).resolve().parents[1] / "bin" / "wax"
loader = importlib.machinery.SourceFileLoader("wax_tx_cli_under_test", str(_CLI_PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


@pytest.mark.parametrize("argv,mode,project", [
    (["tx"], "latest", None),
    (["--json", "tx", "HeyMa"], "latest", "HeyMa"),
    (["tx", "--json", "--project", "Pilot", "--today"], "list", "Pilot"),
    (["tx", "--week", "--limit", "2"], "list", None),
    (["tx", "--yesterday"], "list", None),
])
def test_cli_parser_and_json(argv, mode, project, capsys):
    out = {"schema": 1, "mode": mode, "rows": [], "truncated": False}
    with patch.object(sys, "argv", [str(_CLI_PATH), *argv, "--json"]), \
         patch.object(cli.tx, "run", return_value=out) as run:
        assert cli.main() == 0
    assert json.loads(capsys.readouterr().out) == out
    assert run.call_args.kwargs["project"] == project
    assert run.call_args.kwargs["listing"] == (mode == "list" and "--limit" in argv)
    assert run.call_args.kwargs["window"] == next((w for w in ("today", "yesterday", "week") if "--" + w in argv), None)


def test_cli_positional_and_flag_conflict(capsys):
    with patch.object(sys, "argv", [str(_CLI_PATH), "tx", "Pilot", "--project", "HeyMa", "--json"]):
        assert cli.main() == 2
    assert "positional project or --project" in json.loads(capsys.readouterr().out)["error"]["message"]


@pytest.mark.parametrize("selector", ["--project", "positional"])
@pytest.mark.parametrize("project", ["", "   "])
def test_cli_empty_project_never_falls_back_to_overall(selector, project, capsys):
    argv = [str(_CLI_PATH), "tx", *([selector] if selector == "--project" else []), project, "--json"]
    with patch.object(sys, "argv", argv), \
         patch.object(cli.tx, "run") as run:
        assert cli.main() == 2
    assert "project must not be empty" in json.loads(capsys.readouterr().out)["error"]["message"]
    run.assert_not_called()


def test_cli_empty_latest_error(capsys):
    with patch.object(sys, "argv", [str(_CLI_PATH), "tx", "--json"]), \
         patch.object(cli.tx, "run", side_effect=cli.tx.TxError("no transcript found")):
        assert cli.main() == 2
    assert json.loads(capsys.readouterr().out)["error"]["message"] == "no transcript found"


def test_cli_empty_list_is_explicit(capsys):
    with patch.object(sys, "argv", [str(_CLI_PATH), "tx", "--today"]), \
         patch.object(cli.tx, "run", return_value={"mode": "list", "rows": [], "truncated": False}):
        assert cli.main() == 0
    assert "No transcripts found" in capsys.readouterr().out


def test_cli_plain_content_and_raw(capsys):
    out = {"mode": "latest", "transcript": {"transcript": {"transcript_id": 1}, "version": {"version_no": 1},
                                         "content": "the body", "raw": "---\n---\nthe body"}}
    with patch.object(cli.tx, "run", return_value=out):
        with patch.object(sys, "argv", [str(_CLI_PATH), "tx"]):
            assert cli.main() == 0
        assert "the body" in capsys.readouterr().out
        with patch.object(sys, "argv", [str(_CLI_PATH), "tx", "--raw"]):
            assert cli.main() == 0
        assert capsys.readouterr().out == "---\n---\nthe body"
