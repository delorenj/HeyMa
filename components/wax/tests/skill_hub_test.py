"""Drift guards for HeyMa's hub skill (`skills/heyma/`, skillex ADR-0002).

The hub lives beside the code so it can be tested like code. These tests fail
when the skill and the system disagree: a `wax` subcommand the CLI reference
does not mention, a pass the catalog does not list (or lists with the wrong
enabled state), a link that goes nowhere, a file nothing links to, or a nested
`SKILL.md` that a recursive scanner (Hermes rglobs `SKILL.md`) would surface as
a second skill.
"""

import argparse
import importlib.machinery
import importlib.util
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from wax import failures

COMPONENT_ROOT = next(parent for parent in Path(__file__).resolve().parents
                      if (parent / "pyproject.toml").is_file())
REPO = COMPONENT_ROOT.parents[1]
SKILLS = REPO / "skills"
HUB = SKILLS / "heyma"
PASSES_D = COMPONENT_ROOT / "config" / "passes.d"

_FENCE = re.compile(r"^(```|~~~).*?^\1", re.S | re.M)
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _markdown_files():
    return sorted(p for p in HUB.rglob("*.md") if "__pycache__" not in p.parts)


def _links(md: Path):
    """Relative link targets in a markdown file, code fences excluded."""
    text = _FENCE.sub("", md.read_text())
    for target in _LINK.findall(text):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith(("#", "/", "~")):
            continue
        yield target.split("#", 1)[0]


class _Captured(Exception):
    def __init__(self, parser):
        self.parser = parser


def wax_command_tree():
    """Every `wax <cmd> [<sub>]` path, read from the real argparse tree in bin/wax."""
    loader = importlib.machinery.SourceFileLoader("wax_cli_for_hub_test", str(COMPONENT_ROOT / "bin" / "wax"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)

    def capture(self, *args, **kwargs):
        raise _Captured(self)

    with patch.object(argparse.ArgumentParser, "parse_args", capture), \
            patch.object(argparse.ArgumentParser, "parse_known_args", capture):
        try:
            module.main()
        except _Captured as caught:
            parser = caught.parser
        else:  # pragma: no cover - main() must parse
            raise AssertionError("bin/wax main() never parsed arguments")

    paths = []

    def walk(p, prefix):
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    paths.append(prefix + (name,))
                    walk(child, prefix + (name,))

    walk(parser, ())
    return paths


class HubShapeTest(unittest.TestCase):
    def test_one_hub_and_no_nested_skill_md(self):
        top = sorted(p.parent.name for p in SKILLS.glob("*/SKILL.md"))
        self.assertEqual(top, ["heyma"], "skills/ holds exactly one hub")
        nested = [str(p.relative_to(HUB)) for p in HUB.rglob("SKILL.md") if p.parent != HUB]
        self.assertEqual(nested, [], "sub-skills are SUBSKILL.md; a nested SKILL.md is auto-listed by Hermes")

    def test_frontmatter(self):
        text = (HUB / "SKILL.md").read_text()
        match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        self.assertIsNotNone(match, "SKILL.md needs YAML frontmatter")
        meta = yaml.safe_load(match.group(1))
        self.assertEqual(meta["name"], "heyma")
        self.assertLessEqual(len(meta["description"]), 1024)
        self.assertLessEqual(len(text.splitlines()), 250, "the router stays small; move detail to a reference")
        for sub in HUB.glob("subskills/*/SUBSKILL.md"):
            sub_meta = yaml.safe_load(re.match(r"^---\n(.*?)\n---\n", sub.read_text(), re.S).group(1))
            self.assertTrue(sub_meta.get("name") and sub_meta.get("description"), sub)

    def test_no_symlinks_or_provenance_receipts(self):
        self.assertEqual([str(p) for p in HUB.rglob("*") if p.is_symlink()], [])
        self.assertEqual([str(p) for p in HUB.rglob(".source.yaml")], [])

    def test_every_relative_link_resolves_inside_the_hub(self):
        hub = HUB.resolve()
        problems = []
        for md in _markdown_files():
            for target in _links(md):
                if not target:
                    continue
                dest = (md.parent / target).resolve()
                if hub not in (dest, *dest.parents):
                    problems.append(f"{md.relative_to(HUB)}: {target} leaves the hub")
                elif not dest.exists():
                    problems.append(f"{md.relative_to(HUB)}: {target} does not exist")
        self.assertEqual(problems, [])

    def test_nothing_is_orphaned(self):
        """Every markdown file is reachable from SKILL.md; every other file is named somewhere."""
        seen, todo = set(), [HUB / "SKILL.md"]
        while todo:
            md = todo.pop().resolve()
            if md in seen:
                continue
            seen.add(md)
            for target in _links(md):
                dest = (md.parent / target).resolve()
                if dest.suffix == ".md" and dest.is_file():
                    todo.append(dest)
        unreachable = [str(p.relative_to(HUB)) for p in _markdown_files() if p.resolve() not in seen]
        self.assertEqual(unreachable, [], "link these from the router or a reference")

        corpus = "\n".join(p.read_text() for p in _markdown_files())
        unnamed = [str(p.relative_to(HUB)) for p in HUB.rglob("*")
                   if p.is_file() and p.suffix != ".md"
                   and "__pycache__" not in p.parts and "tests" not in p.relative_to(HUB).parts
                   and p.name not in corpus]
        self.assertEqual(unnamed, [], "scripts and assets must be named by the doc that uses them")


class HubDriftTest(unittest.TestCase):
    def test_cli_reference_names_every_wax_subcommand(self):
        cli = (HUB / "references" / "cli.md").read_text()
        missing = [" ".join(path) for path in wax_command_tree()
                   if not re.search(r"`wax(?: --json)? " + r"\s+".join(map(re.escape, path)) + r"[` ]", cli)]
        self.assertEqual(missing, [], "add these to skills/heyma/references/cli.md")

    def test_pass_catalog_lists_every_pass_with_its_enabled_state(self):
        catalog = (HUB / "references" / "passes" / "catalog.md").read_text()
        rows = {}
        for line in catalog.splitlines():
            if line.startswith("|"):
                for slug in re.findall(r"`([a-z0-9-]+)`", line.split("|")[1]):
                    rows.setdefault(slug, line)
        problems = []
        for path in sorted(PASSES_D.glob("*.yaml")):
            spec = yaml.safe_load(path.read_text())
            slug, enabled = spec["slug"], bool(spec.get("enabled", False))
            row = rows.get(slug)
            if row is None:
                problems.append(f"{slug}: no row whose first cell names it")
                continue
            cells = [c.strip().lower() for c in row.split("|")[1:-1]]
            states = {c for c in cells if c in {"yes", "no", "enabled", "disabled", "on", "off"}}
            want = {"yes", "enabled", "on"} if enabled else {"no", "disabled", "off"}
            if not states or not states <= want:
                problems.append(f"{slug}: registry enabled={enabled}, catalog row says {sorted(states) or 'nothing'}")
        self.assertEqual(problems, [], "fix skills/heyma/references/passes/catalog.md")

    def test_code_that_names_the_hub_names_a_real_file(self):
        self.assertTrue((REPO / failures.DOCTOR_SKILL).is_file(), failures.DOCTOR_SKILL)


if __name__ == "__main__":
    unittest.main()
