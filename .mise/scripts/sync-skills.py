#!/usr/bin/env python3
"""
sync-skills.py — manifest-driven skill fanout.
Replaces the old symlink-based skillex monolithic fanout.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

# The list of agent CLI skill directories (relative to home or project root)
CLI_SKILL_DIRS = [
    ".gemini/skills",
    ".codex/skills",
    ".kimi/skills",
    ".augment/skills",
    ".config/opencode/skills",
    ".hermes/skills",
    ".claude/skills",
    ".openclaw/skills",
]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Sync skills from manifest to agent CLIs."
    )
    parser.add_argument(
        "--scope",
        choices=["global", "project"],
        required=True,
        help="Whether to sync global skills or project-local skills.",
    )
    parser.add_argument("--project-root", help="Explicit CommonProject root")
    return parser.parse_args()


def resolve_project_root(explicit=None):
    if explicit:
        root = Path(explicit).expanduser().resolve(strict=True)
        if not (root / ".project.json").is_file():
            raise ValueError(f"Project root has no .project.json: {root}")
        return root
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".project.json").is_file():
            return candidate
    raise ValueError("Cannot locate project root from sync script")


def load_manifest(manifest_path):
    if not manifest_path.exists():
        return {"skills": []}
    with open(manifest_path, "r") as handle:
        return json.load(handle)


def validate_skill_name(name):
    if not isinstance(name, str) or not name:
        raise ValueError("Skill name must be a non-empty string")
    if name in {".", ".."} or Path(name).is_absolute():
        raise ValueError(f"Unsafe skill name: {name!r}")
    if "/" in name or "\\" in name or Path(name).name != name:
        raise ValueError(f"Skill name must be one path component: {name!r}")
    return name


def resolve_registry_path(registry_root, registry_path):
    if not isinstance(registry_path, str) or not registry_path:
        raise ValueError("registry_path must be a non-empty relative path")
    relative = Path(registry_path)
    if relative.is_absolute():
        raise ValueError(f"registry_path must be relative: {registry_path!r}")
    root = Path(registry_root).resolve(strict=True)
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f"registry_path escapes registry root: {registry_path!r}") from error
    return resolved


def manifest_skill_name(skill):
    if isinstance(skill, str):
        path = skill if "/" in skill else f"all-skills/{skill}"
        return validate_skill_name(path.split("/")[-1])
    if not isinstance(skill, dict):
        raise ValueError(f"Skill entry must be a string or object: {skill!r}")
    return validate_skill_name(skill.get("name"))


def validate_manifest_skill_names(manifest):
    skills = manifest.get("skills", [])
    if not isinstance(skills, list):
        raise ValueError("Manifest skills must be an array")
    for skill in skills:
        manifest_skill_name(skill)


def assert_real_directory_chain(root, target):
    root = root.resolve(strict=True)
    target = target.absolute()
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise ValueError(f"Destination escapes root {root}: {target}") from error
    current = root
    for part in relative.parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            break
        if current.is_symlink():
            raise ValueError(f"Refusing symlinked destination directory: {current}")
        if not current.is_dir():
            raise ValueError(f"Destination parent is not a directory: {current}")


def preflight_cli_dirs(cli_dirs_base, skill_names):
    base = cli_dirs_base.resolve(strict=True)
    canonical_project_skills = base / ".agents" / "skills"
    active = []
    for cli_rel_path in CLI_SKILL_DIRS:
        cli_dir = base / cli_rel_path
        parent = cli_dir.parent
        if not parent.exists() and not parent.is_symlink():
            continue
        assert_real_directory_chain(base, parent)
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError(f"Unsafe CLI destination parent: {parent}")
        if (
            cli_dir.is_symlink()
            and cli_dir.resolve(strict=True) == canonical_project_skills
        ):
            # Some CLIs use .agents/skills as their canonical shared project
            # directory. It is already populated and must not be fanned out
            # onto itself.
            continue
        if cli_dir.is_symlink():
            raise ValueError(f"Refusing symlinked CLI skills directory: {cli_dir}")
        if cli_dir.exists() and not cli_dir.is_dir():
            raise ValueError(f"CLI skills destination is not a directory: {cli_dir}")
        real_parent = parent.resolve(strict=True)
        expected_cli = real_parent / cli_dir.name
        if cli_dir.exists() and cli_dir.resolve(strict=True) != expected_cli:
            raise ValueError(f"CLI skills directory escapes its parent: {cli_dir}")
        for name in skill_names:
            destination = expected_cli / name
            if destination.parent != expected_cli or len(destination.relative_to(expected_cli).parts) != 1:
                raise ValueError(f"Skill destination escapes CLI directory: {destination}")
        active.append(cli_dir)
    return active


def ensure_cache_dir():
    cache_dir = Path(os.path.expanduser("~/.agents/.cache/skills"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def sync_registry(registry_url):
    # Sanitize registry_url to create a folder name
    safe_name = re.sub(r"[^a-zA-Z0-9]", "_", registry_url)
    cache_dir = (
        Path(os.path.expanduser("~/.agents/.cache/registries")) / safe_name
    )

    if cache_dir.exists():
        try:
            print(f"Updating registry {registry_url}...")
            subprocess.run(
                ["git", "-C", str(cache_dir), "pull"],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as error:
            print(
                f"Warning: Failed to update registry {registry_url}: "
                f"{error.stderr.decode()}",
                file=sys.stderr,
            )
    else:
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        print(f"Cloning registry {registry_url}...")
        subprocess.run(["git", "clone", registry_url, str(cache_dir)], check=True)

    return cache_dir


def sync_git_skill(name, source, version, cache_dir):
    target_dir = cache_dir / name
    if target_dir.exists():
        # Just pull if it exists
        try:
            print(f"Updating git skill {name} in cache...")
            subprocess.run(
                ["git", "-C", str(target_dir), "pull"],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as error:
            print(
                f"Warning: Failed to update {name}: {error.stderr.decode()}",
                file=sys.stderr,
            )
    else:
        print(f"Cloning git skill {name} to cache...")
        subprocess.run(["git", "clone", source, str(target_dir)], check=True)

    if version:
        subprocess.run(
            ["git", "-C", str(target_dir), "checkout", version], check=True
        )

    return target_dir


def resolve_skill_path(
    skill, cache_dir, base_dir, default_registry, registry_cache
):
    if isinstance(skill, str):
        path = skill if "/" in skill else f"all-skills/{skill}"
        name = validate_skill_name(path.split("/")[-1])
        skill = {"name": name, "registry_path": path}

    name = validate_skill_name(skill.get("name"))

    if "registry_path" in skill:
        registry_url = skill.get("registry", default_registry)
        if registry_url not in registry_cache:
            registry_cache[registry_url] = sync_registry(registry_url)
        full_path = resolve_registry_path(
            registry_cache[registry_url], skill["registry_path"]
        )
        if not full_path.exists():
            print(
                f"Warning: Registry skill {name} not found at {full_path}",
                file=sys.stderr,
            )
            return name, None
        return name, full_path

    source = skill.get("source", "")
    if source.startswith("git@") or source.startswith("https://"):
        return name, sync_git_skill(
            name, source, skill.get("version"), cache_dir
        )
    parsed = urlparse(source)
    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"}:
            raise ValueError(f"Non-local file URI authority for skill {name}: {parsed.netloc}")
        if parsed.query or parsed.fragment:
            raise ValueError(f"file:// source must encode query/fragment characters: {source}")
        local_path = Path(unquote(parsed.path))
        full_path = (base_dir / local_path).resolve()
        if not full_path.exists():
            print(
                f"Warning: Local skill {name} not found at {full_path}",
                file=sys.stderr,
            )
            return name, None
        return name, full_path

    print(
        f"Warning: Unknown source type for skill {name}: {source}",
        file=sys.stderr,
    )
    return name, None


def fanout_to_cli(cli_dirs_base, skills_map):
    """
    Creates symlinks in each of the CLI_SKILL_DIRS relative to cli_dirs_base
    pointing to the resolved paths in skills_map.
    """
    skill_names = [validate_skill_name(name) for name in skills_map]
    active_cli_dirs = preflight_cli_dirs(cli_dirs_base, skill_names)
    linked_total = 0
    for cli_dir in active_cli_dirs:
        cli_dir.mkdir(parents=True, exist_ok=True)
        if cli_dir.is_symlink() or not cli_dir.is_dir():
            raise ValueError(f"Unsafe CLI skills directory after creation: {cli_dir}")
        real_cli_dir = cli_dir.resolve(strict=True)

        for name, actual_path in skills_map.items():
            symlink_target = real_cli_dir / name
            if symlink_target.parent != real_cli_dir:
                raise ValueError(f"Skill destination escapes CLI directory: {symlink_target}")

            # If it's a symlink already pointing to the right place, skip
            if (
                symlink_target.is_symlink()
                and os.readlink(symlink_target) == str(actual_path)
            ):
                continue

            # If it exists but is wrong, remove it
            if symlink_target.exists() or symlink_target.is_symlink():
                if symlink_target.is_dir() and not symlink_target.is_symlink():
                    shutil.rmtree(symlink_target)
                else:
                    symlink_target.unlink()

            os.symlink(actual_path, symlink_target)
            linked_total += 1
            print(f"→ {symlink_target} -> {actual_path}")

    print(
        f"sync-skills: {linked_total} new/updated symlink(s) "
        f"across CLIs in {cli_dirs_base}"
    )


def main():
    args = parse_args()
    cache_dir = ensure_cache_dir()

    global_manifest_path = Path(os.path.expanduser("~/.agents/skills.json"))
    project_root = resolve_project_root(args.project_root)
    project_manifest_path = project_root / ".agents" / "skills.json"

    skills_to_sync = {}  # name -> actual_path
    registry_cache = {}

    if args.scope == "global":
        print(f"Loading global manifest from {global_manifest_path}")
        manifest = load_manifest(global_manifest_path)
        validate_manifest_skill_names(manifest)
        default_registry = manifest.get(
            "registry", "https://github.com/delorenj/skillex.git"
        )
        base_dir = global_manifest_path.parent
        for skill in manifest.get("skills", []):
            name, path = resolve_skill_path(
                skill,
                cache_dir,
                base_dir,
                default_registry,
                registry_cache,
            )
            if path:
                skills_to_sync[name] = path

        # Fanout globally (home dir)
        fanout_to_cli(Path(os.path.expanduser("~")), skills_to_sync)

    elif args.scope == "project":
        print(f"Loading project manifest from {project_manifest_path}")
        manifest = load_manifest(project_manifest_path)
        validate_manifest_skill_names(manifest)
        default_registry = manifest.get(
            "registry", "https://github.com/delorenj/skillex.git"
        )

        # Check if we should inherit global skills
        if manifest.get("inherit_global", False):
            print("Inheriting global skills...")
            global_manifest = load_manifest(global_manifest_path)
            validate_manifest_skill_names(global_manifest)
            global_registry = global_manifest.get(
                "registry", "https://github.com/delorenj/skillex.git"
            )
            for skill in global_manifest.get("skills", []):
                name, path = resolve_skill_path(
                    skill,
                    cache_dir,
                    global_manifest_path.parent,
                    global_registry,
                    registry_cache,
                )
                if path:
                    skills_to_sync[name] = path

        base_dir = project_manifest_path.parent
        for skill in manifest.get("skills", []):
            name, path = resolve_skill_path(
                skill,
                cache_dir,
                base_dir,
                default_registry,
                registry_cache,
            )
            if path:
                # Overrides global skill of the same name
                skills_to_sync[name] = path

        # Fanout locally (project dir)
        fanout_to_cli(project_root, skills_to_sync)


if __name__ == "__main__":
    main()
