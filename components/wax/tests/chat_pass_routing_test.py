"""Chat passes route through the AutomaticAI gateway, never at OpenRouter.

On 2026-10-21 title-slug and classification exhausted their attempts with
`HTTP 429 Insufficient credits` because their model was a gateway OpenRouter
passthrough (`automaticai/openrouter/...`) onto a drained OpenRouter account.
This guards the registry, the bin defaults a bare-shell run falls back to, and
the doctor's defaults against that route coming back, or against a pass
reaching OpenRouter directly or picking up `OPENROUTER_API_KEY`.

A chat pass is one whose YAML env declares a `*_API_BASE`. project-extraction
(Jev, `WAX_JEV_*`) is not one: its OpenRouter decisions call is a separate,
pending decision.
"""

import re
import unittest
from pathlib import Path

import yaml

COMPONENT_ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)
PASSES_DIR = COMPONENT_ROOT / "config" / "passes.d"
GATEWAY = "https://api.automaticai.io/v1"
# A module-level DEFAULT_* / *_DEFAULT string constant: what a pass or the
# doctor uses when the YAML env does not say otherwise.
DEFAULT_CONST = re.compile(r'^(?:DEFAULT_\w+|\w+_DEFAULT)\s*=\s*"([^"]*)"', re.MULTILINE)


def _forbidden(value: str) -> str:
    lowered = value.lower()
    if "openrouter.ai" in lowered:
        return "points at openrouter.ai"
    if lowered.startswith(("automaticai/openrouter/", "aai/openrouter/")):
        return "is a gateway OpenRouter passthrough route"
    return ""


def chat_passes() -> dict[str, dict]:
    out = {}
    for path in sorted(PASSES_DIR.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text()) or {}
        env = doc.get("env") or {}
        if any(str(key).endswith("_API_BASE") for key in env):
            out[doc.get("slug") or path.stem] = doc
    return out


class ChatPassRoutingTest(unittest.TestCase):
    def test_the_known_chat_passes_are_covered(self):
        self.assertTrue({"title-slug", "classification", "ticketable"} <= set(chat_passes()))

    def test_yaml_env_routes_through_the_gateway(self):
        for slug, doc in chat_passes().items():
            env = {str(k): str(v) for k, v in (doc.get("env") or {}).items()}
            self.assertNotIn("OPENROUTER_API_KEY", env, slug)
            for key, value in env.items():
                with self.subTest(pass_=slug, var=key):
                    self.assertEqual(_forbidden(value), "", f"{slug} {key}={value}")
                    if key.endswith("_API_BASE"):
                        self.assertEqual(value.rstrip("/"), GATEWAY)

    def test_bin_defaults_never_reach_openrouter(self):
        scripts = {PASSES_DIR / "bin" / Path(str(doc["command"][0])).name
                   for doc in chat_passes().values()}
        # classification borrows title-slug's defaults; check both regardless.
        scripts.add(PASSES_DIR / "bin" / "title-slug")
        scripts.add(COMPONENT_ROOT / "bin" / "wax")
        for script in sorted(scripts):
            source = script.read_text()
            with self.subTest(script=script.name):
                if script.name != "wax":
                    # The doctor may name the variable to report it; a pass must not read it.
                    self.assertNotIn("OPENROUTER_API_KEY", source)
                for value in DEFAULT_CONST.findall(source):
                    self.assertEqual(_forbidden(value), "", f"{script.name}: {value}")
        self.assertNotIn('"OPENROUTER_API_KEY"', (COMPONENT_ROOT / "bin" / "wax").read_text(),
                         "the doctor must not advertise OPENROUTER_API_KEY as a title-slug credential")

    def test_asr_has_no_direct_groq_path(self):
        # transcribe.py once carried a `--groq` branch posting audio straight to
        # api.groq.com with a raw GROQ_API_KEY. Nothing called it; it was removed
        # on 2026-10-22. ASR is local faster-whisper; a hosted route belongs on
        # the gateway, never a direct vendor call.
        repo_root = COMPONENT_ROOT.parents[1]
        for rel in ("scripts/transcribe.py", "bin/transcribe"):
            source = (repo_root / rel).read_text().lower()
            with self.subTest(file=rel):
                for needle in ("api.groq.com", "groq_api_key", "--groq"):
                    self.assertNotIn(needle, source, f"{rel} reintroduced {needle}")

    def test_regression_check_catches_the_old_route(self):
        self.assertTrue(_forbidden("automaticai/openrouter/google/gemini-3.7-flash"))
        self.assertTrue(_forbidden("https://openrouter.ai/api/v1"))
        self.assertEqual(_forbidden("automaticai/auto/fast"), "")


if __name__ == "__main__":
    unittest.main()
