#!/usr/bin/env python3
"""Project-owned projection of the shared AutomaticAI Codex installer."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import importlib.util
import io
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_EFFORT = "xhigh"


def load_owner():
    source = Path.home() / ".agents/providers/automaticai/codex-gateway.py"
    specification = importlib.util.spec_from_file_location("automaticai_codex_owner", source)
    if specification is None or specification.loader is None:
        raise RuntimeError("the shared AutomaticAI installer is unavailable")
    owner = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(owner)
    return owner


def project_home(root: Path) -> Path:
    return root.resolve() / ".codex"


def synchronize(owner, root: Path, check: bool = False) -> dict:
    home = project_home(root)
    config_path = home / "config.toml"
    config = owner.read_toml(config_path)
    catalog = home / "cache/automaticai.models.json"
    if check:
        if (config.get("model_provider") != "automaticai"
                or config.get("daemon_auto_start") is not False
                or config.get("model_catalog_json") != str(catalog)):
            raise owner.MigrationError("project provider, catalog or standalone launch settings have drifted")
        commands = ["check", "--codex-root", str(home)]
    else:
        if config.get("model_provider", "openai") not in {"openai", "automaticai"}:
            raise owner.MigrationError("another project provider owns inference; coordinate its migration first")
        if not config.get("model"):
            route = owner.select_route(DEFAULT_MODEL, owner.load_catalog(owner.CATALOG))
            template = owner.read_toml(owner.SOURCE / "codex-provider.toml")
            owner.require_available(route, owner.token_reference(template["model_providers"]["automaticai"]))
            original = config_path.read_text() if config_path.exists() else ""
            seeded = owner.replace_root_settings(original, {
                "model": DEFAULT_MODEL,
                "model_reasoning_effort": config.get("model_reasoning_effort") or DEFAULT_EFFORT,
            })
            owner.atomic_write(config_path, seeded, expected=original)
        if config.get("model_provider", "openai") == "openai":
            for command in ("install", "activate"):
                if owner.main([command, "--codex-root", str(home)]) != 0:
                    raise owner.MigrationError("shared installer refused project activation; no native fallback")
        commands = ["install", "--codex-root", str(home)]
    if owner.main(commands) != 0:
        raise owner.MigrationError("shared installer could not verify the project projection")
    original = config_path.read_text()
    template = (owner.SOURCE / "codex-provider.toml").read_text()
    projected = owner.replace_root_settings(owner.register_provider(original, template), {
        "daemon_auto_start": False,
    })
    if check and projected != original:
        raise owner.MigrationError("project provider projection has drifted")
    if not check:
        owner.atomic_write(config_path, projected, expected=original)
    config = owner.read_toml(config_path)
    return {
        "codex_home": str(home), "provider": config["model_provider"],
        "model": config["model"], "effort": config["model_reasoning_effort"],
        "models": len(json.loads(catalog.read_text())["models"]),
        "daemon_auto_start": config["daemon_auto_start"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        owner = load_owner()
        home = project_home(ROOT)
        home.mkdir(parents=True, exist_ok=True)
        with (home / "automaticai-install.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with contextlib.redirect_stdout(io.StringIO()):
                result = synchronize(owner, ROOT, arguments.check)
        if not arguments.quiet:
            print(json.dumps(result, indent=2))
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, (RuntimeError, owner.MigrationError) if "owner" in locals() else RuntimeError) else "source/configuration operation failed; no contents displayed"
        print("HeyMa AutomaticAI: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
