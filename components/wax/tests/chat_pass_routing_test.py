"""Chat passes route through the AutomaticAI gateway, never at OpenRouter.

On 2026-10-21 title-slug and classification exhausted their attempts with
`HTTP 429 Insufficient credits` because their model was a gateway OpenRouter
passthrough (`automaticai/openrouter/...`) onto a drained OpenRouter account.
This guards the registry, the bin defaults a bare-shell run falls back to, and
the doctor's defaults against that route coming back, or against a pass
reaching OpenRouter directly or picking up `OPENROUTER_API_KEY`.

A chat pass is one whose YAML env declares a `*_API_BASE`. project-extraction
(Jev, `WAX_JEV_*`) is not one: Jev has no gateway route, so it goes through
the fleet's Jev ingress (jev-litellm at jev.delo.sh) instead. It sat on
OpenRouter's alpha/decisions proxy until 2026-10-22, when that account was
already dead; JevRoutingTest keeps it, and every other pass, off OpenRouter.
"""

import ast
import importlib.util
import io
import json
import os
import re
import tempfile
import unittest
import urllib.error
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

import yaml

COMPONENT_ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)
PASSES_DIR = COMPONENT_ROOT / "config" / "passes.d"
GATEWAY = "https://api.automaticai.io/v1"
JEV_INGRESS = "https://jev.delo.sh/typesafe/v1/systemone"
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


# Operator order, 2026-10-22: "wax inference: DO NOT use anthropic models."
# Nothing Wax sends to a model may name one: not a pass env value, not a bin or
# library default, not the token scope, not the doctor's rows, not an authored
# pipeline. Hyper ("hyper crush") and the free family are the fallbacks.
ANTHROPIC = re.compile(r"claude|sonnet|haiku|opus|fable|anthropic", re.IGNORECASE)
PIPELINES_DIR = COMPONENT_ROOT / "config" / "pipelines.d"
N8N_EXPORTS = sorted((COMPONENT_ROOT.parents[1] / "components" / "n8n-nodes-heyma" / "examples").glob("*.json"))


def anthropic_hits(value: str) -> list[str]:
    """The model names inside one value (a comma-separated list counts per member)."""
    return [part.strip() for part in str(value).split(",") if ANTHROPIC.search(part)]


def _strings(node: ast.AST):
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            yield child.value


def model_literals(path: Path) -> list[tuple[str, str]]:
    """(where, string) for every model-ish literal in a Python file, comments excluded.

    Model-ish: assigned to a name containing MODEL or starting DEFAULT_, the
    default argument of os.environ.get("..MODEL..", "x"), or any string in a
    tuple whose first element names a *_MODEL variable (the doctor's rows).
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any("MODEL" in n.upper() or n.startswith("DEFAULT_") for n in names):
                found += [(f"{path.name}:{node.lineno} {names[0]}", v) for v in _strings(node.value)]
        elif isinstance(node, ast.Call) and node.args:
            first = node.args[0]
            if (isinstance(first, ast.Constant) and isinstance(first.value, str) and "MODEL" in first.value.upper()):
                found += [(f"{path.name}:{node.lineno} {first.value}", v)
                          for arg in node.args[1:] for v in _strings(arg)]
        elif isinstance(node, ast.Tuple) and node.elts:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) and "_MODEL" in first.value:
                found += [(f"{path.name}:{node.lineno} {first.value}", v) for v in _strings(node)][1:]
    return found


def load_script(path: Path):
    loader = SourceFileLoader(f"scan_{path.name.replace('-', '_')}", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class NoAnthropicModelsTest(unittest.TestCase):
    def test_no_pass_yaml_env_value_names_an_anthropic_model(self):
        # Every pass, enabled or not: a disabled pass is one flag from running.
        for path in sorted(PASSES_DIR.glob("*.yaml")):
            env = (yaml.safe_load(path.read_text()) or {}).get("env") or {}
            for key, value in env.items():
                with self.subTest(pass_=path.stem, var=key):
                    self.assertEqual(anthropic_hits(value), [], f"{path.name} {key}={value}")

    def test_bin_and_library_defaults_name_no_anthropic_model(self):
        sources = sorted(p for p in (PASSES_DIR / "bin").iterdir() if p.is_file())
        sources += sorted((COMPONENT_ROOT / "src" / "wax").glob("*.py"))
        sources.append(COMPONENT_ROOT / "bin" / "wax")
        checked = 0
        for path in sources:
            try:
                literals = model_literals(path)
            except SyntaxError:
                continue  # a non-Python helper in bin/
            for where, value in literals:
                checked += 1
                with self.subTest(where=where):
                    self.assertEqual(anthropic_hits(value), [], f"{where}: {value!r}")
        self.assertGreater(checked, 10, "the scan found almost no model literals; its matcher is broken")

    def test_provision_inference_scope_names_no_anthropic_model(self):
        module = load_script(COMPONENT_ROOT / "deploy" / "provision-inference")
        self.assertEqual(anthropic_hits(",".join(module.MODELS)), [])
        for where, value in model_literals(COMPONENT_ROOT / "deploy" / "provision-inference"):
            with self.subTest(where=where):
                self.assertEqual(anthropic_hits(value), [], f"{where}: {value!r}")
        # The route list in the source, however it is spelled.
        source = (COMPONENT_ROOT / "deploy" / "provision-inference").read_text()
        for match in re.findall(r'^\s+"(automaticai/[^"]+)",?\s*$', source, re.MULTILINE):
            self.assertEqual(anthropic_hits(match), [], match)

    def test_provision_inference_refuses_an_anthropic_route(self):
        module = load_script(COMPONENT_ROOT / "deploy" / "provision-inference")
        for banned in ("automaticai/damien/claude-haiku-4.5", "automaticai/personal/claude-sonnet-5.5",
                       "automaticai/damien/claude-opus-5", "automaticai/damien/claude-fable-5"):
            with self.assertRaises(RuntimeError):
                module.plan([module.ROUTE, banned], {module.ROUTE, banned})

    def test_provision_inference_skips_a_route_the_catalog_lacks(self):
        module = load_script(COMPONENT_ROOT / "deploy" / "provision-inference")
        available = set(module.MODELS) - {"automaticai/hypercharm/minimax-m3"}
        models, skipped = module.plan(module.MODELS, available)
        self.assertEqual(skipped, ["automaticai/hypercharm/minimax-m3"])
        self.assertNotIn("automaticai/hypercharm/minimax-m3", models)
        self.assertIn(module.ROUTE, models)

    def test_provision_inference_keeps_kimi_and_openrouter_out(self):
        module = load_script(COMPONENT_ROOT / "deploy" / "provision-inference")
        for name in module.MODELS:
            self.assertNotIn("kimi", name.lower(), "AAI-55: Kimi 400s on temperature != 1")
            self.assertNotIn("openrouter", name.lower(), name)

    def test_authored_pipelines_and_n8n_exports_name_no_anthropic_model(self):
        files = sorted(PIPELINES_DIR.glob("*.yaml")) + N8N_EXPORTS
        self.assertTrue(files and N8N_EXPORTS, "found no pipeline sources to scan")
        for path in files:
            with self.subTest(file=path.name):
                text = path.read_text()
                self.assertEqual(ANTHROPIC.findall(text), [], f"{path.name} names an Anthropic model")

    def test_compiled_frozen_definitions_name_no_anthropic_model(self):
        # Compiled pipelines FREEZE pass env, so a stale compile keeps serving
        # whatever model it captured. Compile every authored pipeline and import
        # every n8n release export into a scratch ledger, then scan what froze.
        import tempfile
        from unittest.mock import patch
        from wax import events, passes, paths, pipelines
        from documents_test import _close_test_connection

        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            stack = [patch.object(paths, "DB", runtime / "wax.db"), patch.object(paths, "VAR", runtime),
                     patch.object(passes, "REGISTRY_DIR", PASSES_DIR),
                     patch.object(pipelines, "PIPELINES_DIR", PIPELINES_DIR),
                     patch.object(events, "emit")]
            for item in stack:
                item.start()
            _close_test_connection()
            try:
                pipelines.compile()
                for export in N8N_EXPORTS:
                    if "release" in export.name:
                        pipelines.import_n8n(export)
                frozen = pipelines.compiled()
                self.assertIn("transcript-standard", frozen)
                self.assertIn("doc-standard", frozen)
                models_seen = 0
                for name, definition in frozen.items():
                    text = json.dumps(definition)
                    models_seen += text.count("WAX_TITLE_MODEL") + text.count("WAX_TICKETS_MODEL")
                    with self.subTest(pipeline=name):
                        self.assertEqual(ANTHROPIC.findall(text), [], f"{name} froze an Anthropic model")
                self.assertGreater(models_seen, 0, "no frozen definition carried a model; the scan sees nothing")
            finally:
                for item in stack:
                    item.stop()
                _close_test_connection()

    def test_the_matcher_catches_what_it_should(self):
        for name in ("automaticai/damien/claude-haiku-4.5", "automaticai/personal/claude-sonnet-5.5",
                     "anthropic/claude-3-opus", "AUTOMATICAI/DAMIEN/CLAUDE-OPUS-5", "x/fable-5.1",
                     "automaticai/auto/fast,automaticai/damien/claude-haiku-4.5"):
            self.assertTrue(anthropic_hits(name), name)
        for name in ("automaticai/auto/fast", "automaticai/hypercharm/glm-5.3",
                     "automaticai/hypercharm/minimax-m3", "automaticai/free",
                     "automaticai/personal/glm-5.3-flash"):
            self.assertEqual(anthropic_hits(name), [], name)


CHAIN = ["automaticai/auto/fast", "automaticai/hypercharm/glm-5.3",
         "automaticai/hypercharm/minimax-m3", "automaticai/free"]
NOTE_TEXT = "---\nlanguage: en\n---\n# Transcript\nDiscussing a modular enrichment pass for audio.\n"


def _http_error(status: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.automaticai.io/v1/chat/completions", status, "x", {},
                                  io.BytesIO(body.encode()))


def _chat_answer(content: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(content)}}]}


class FakeGateway:
    """Stands in for title-slug._api_request: a /models list and a scripted answer per model."""

    def __init__(self, answers: dict, listed=None):
        self.answers = answers
        self.listed = CHAIN if listed is None else listed
        self.chat_models: list = []

    def __call__(self, url, *, key, model, timeout, payload=None):
        if payload is None:
            return {"data": [{"id": name} for name in self.listed]}
        self.chat_models.append(payload["model"])
        outcome = self.answers[payload["model"]]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FallbackChainTest(unittest.TestCase):
    """Every chat pass tries auto/fast, then Hyper, then the free family; and says who served."""

    def setUp(self):
        self.title = load_script(PASSES_DIR / "bin" / "title-slug")
        self.title._NOTES.clear()
        self.addCleanup(self.title._NOTES.clear)
        env = patch.dict(os.environ, {"WAX_TITLE_MODEL": "", "WAX_TITLE_API_BASE": "",
                                      "WAX_TITLE_MODEL_FALLBACKS": ""} )
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("WAX_TITLE_MODEL_FALLBACKS")  # unset: the bin default applies

    def note(self, directory: str, text: str = NOTE_TEXT) -> Path:
        path = Path(directory) / "recording.md"
        path.write_text(text)
        return path

    def run_title(self, gateway: FakeGateway, text: str = NOTE_TEXT):
        with tempfile.TemporaryDirectory() as directory:
            md = self.note(directory, text)
            with patch.object(self.title, "resolve_api_key", return_value="test-key"), \
                    patch.object(self.title, "_api_request", side_effect=gateway):
                result = self.title.build_result(md)
            self.assertEqual(md.read_text(), text, "a pass never edits its note")
        return result

    GOOD = _chat_answer({"title": "Modular Enrichment Pass", "summary": "A pass for audio."})

    def test_the_default_chain_is_auto_then_hyper_then_free(self):
        self.assertEqual(self.title.model_chain(), CHAIN)
        for slug in ("title-slug", "classification"):
            env = yaml.safe_load((PASSES_DIR / f"{slug}.yaml").read_text())["env"]
            self.assertEqual([env["WAX_TITLE_MODEL"], *env["WAX_TITLE_MODEL_FALLBACKS"].split(",")], CHAIN)
        env = yaml.safe_load((PASSES_DIR / "ticketable.yaml").read_text())["env"]
        self.assertEqual([env["WAX_TICKETS_MODEL"], *env["WAX_TICKETS_MODEL_FALLBACKS"].split(",")], CHAIN)
        ticketable = load_script(PASSES_DIR / "bin" / "ticketable")
        self.assertEqual([ticketable.DEFAULT_MODEL, *ticketable.DEFAULT_MODEL_FALLBACKS.split(",")], CHAIN)

    def test_every_chain_fits_inside_its_pass_timeout(self):
        # Worst case: every member burns its whole request timeout.
        for slug, var, windows in (("title-slug", "WAX_TITLE_REQUEST_TIMEOUT_S", 1),
                                   ("classification", "WAX_TITLE_REQUEST_TIMEOUT_S", 1),
                                   ("ticketable", "WAX_TICKETS_REQUEST_TIMEOUT_S", 3)):
            doc = yaml.safe_load((PASSES_DIR / f"{slug}.yaml").read_text())
            worst = windows * len(CHAIN) * float(doc["env"][var])
            self.assertLess(worst, float(doc["timeout_s"]), f"{slug}: {worst}s worst case vs timeout_s")

    def test_primary_serves_and_the_evidence_names_it(self):
        gateway = FakeGateway({CHAIN[0]: self.GOOD})
        result = self.run_title(gateway)
        self.assertEqual(gateway.chat_models, [CHAIN[0]])
        self.assertEqual(result["evidence"], {"model": CHAIN[0], "chain": CHAIN, "fallbacks": []})

    def test_a_429_on_the_primary_falls_to_hyper(self):
        gateway = FakeGateway({CHAIN[0]: self.title._http_failure(_http_error(429, "slow"), url="u", model=CHAIN[0]),
                               CHAIN[1]: self.GOOD})
        result = self.run_title(gateway)
        self.assertEqual(gateway.chat_models, CHAIN[:2])
        self.assertEqual(result["evidence"]["model"], "automaticai/hypercharm/glm-5.3")
        self.assertEqual(result["evidence"]["fallbacks"],
                         [{"model": CHAIN[0], "reason_code": "provider_rate_limited", "status": 429}])

    def test_hyper_failing_too_falls_to_the_free_family(self):
        gateway = FakeGateway({
            CHAIN[0]: self.title._http_failure(_http_error(429), url="u", model=CHAIN[0]),
            CHAIN[1]: self.title._http_failure(
                _http_error(503, '{"error":{"code":"model_not_found","message":"No available channel"}}'),
                url="u", model=CHAIN[1]),
            CHAIN[2]: self.title._http_failure(_http_error(500), url="u", model=CHAIN[2]),
            CHAIN[3]: self.GOOD})
        result = self.run_title(gateway)
        self.assertEqual(gateway.chat_models, CHAIN)
        self.assertEqual(result["evidence"]["model"], "automaticai/free")
        self.assertEqual([f["reason_code"] for f in result["evidence"]["fallbacks"]],
                         ["provider_rate_limited", "provider_http_error", "provider_http_error"])

    def test_each_provider_side_failure_falls_through(self):
        failures = {
            "429": self.title._http_failure(_http_error(429), url="u", model="m"),
            "402": self.title._http_failure(_http_error(402), url="u", model="m"),
            "500": self.title._http_failure(_http_error(500), url="u", model="m"),
            "400": self.title._http_failure(_http_error(400, "bad temperature"), url="u", model="m"),
            "missing_model": self.title._http_failure(_http_error(404, "model m not found"), url="u", model="m"),
            "403 out of scope": self.title._http_failure(
                _http_error(403, '{"error":{"message":"This token has no access to model m"}}'), url="u", model="m"),
            "timeout": self.title.TitlePassError("slow", reason_code="timeout"),
            "unreachable": self.title.TitlePassError("down", reason_code="provider_unreachable"),
            "unparseable": self.title.TitlePassError("junk", reason_code="provider_bad_response"),
        }
        for label, failure in failures.items():
            with self.subTest(label):
                result = self.run_title(FakeGateway({CHAIN[0]: failure, CHAIN[1]: self.GOOD}))
                self.assertEqual(result["evidence"]["model"], CHAIN[1])

    def test_empty_content_falls_through(self):
        empty = _chat_answer({"title": "", "summary": ""})
        result = self.run_title(FakeGateway({CHAIN[0]: empty, CHAIN[1]: self.GOOD}))
        self.assertEqual(result["evidence"]["model"], CHAIN[1])

    def test_a_rejected_key_does_not_fall_through(self):
        # 401, and a 403 that is not about the model's scope, are about the key
        # every member shares: stop at once instead of asking four times.
        for status, body in ((401, "User not found."), (403, "forbidden")):
            with self.subTest(status=status):
                gateway = FakeGateway({CHAIN[0]: self.title._http_failure(
                    _http_error(status, body), url="u", model=CHAIN[0]), CHAIN[1]: self.GOOD})
                with self.assertRaises(self.title.ProviderError) as caught:
                    self.run_title(gateway)
                self.assertEqual(caught.exception.reason_code, "provider_auth_rejected")
                self.assertEqual(gateway.chat_models, [CHAIN[0]])

    def test_no_api_key_does_not_fall_through(self):
        gateway = FakeGateway({name: self.GOOD for name in CHAIN})
        with tempfile.TemporaryDirectory() as directory:
            md = self.note(directory)
            with patch.object(self.title, "resolve_api_key",
                              side_effect=self.title.TitlePassError("no key", reason_code="no_api_key")), \
                    patch.object(self.title, "_api_request", side_effect=gateway):
                with self.assertRaises(self.title.ProviderError) as caught:
                    self.title.build_result(md)
        self.assertEqual(caught.exception.reason_code, "no_api_key")
        self.assertEqual(gateway.chat_models, [], "no model may be asked without a key")

    def test_no_api_key_inside_the_chain_stops_it(self):
        from wax import provider
        calls = []

        def call(model):
            calls.append(model)
            raise provider.ProviderError("key gone", "no_api_key")

        with self.assertRaises(provider.ChainFailed) as caught:
            provider.run_chain(CHAIN, call)
        self.assertEqual((caught.exception.reason_code, calls), ("no_api_key", [CHAIN[0]]))

    def test_an_unlisted_member_is_skipped_without_a_request(self):
        # The preflight never blocks the chain: a primary the key cannot see is
        # recorded as missing_model and the next member serves.
        gateway = FakeGateway({CHAIN[1]: self.GOOD}, listed=CHAIN[1:])
        result = self.run_title(gateway)
        self.assertEqual(gateway.chat_models, [CHAIN[1]])
        self.assertEqual(result["evidence"]["model"], CHAIN[1])
        self.assertEqual(result["evidence"]["fallbacks"][0]["reason_code"], "missing_model")

    def test_the_free_family_is_never_skipped_for_being_unlisted(self):
        # /v1/models does not list automaticai/free even when the token is
        # granted it; the preflight must still let the chain reach it.
        gateway = FakeGateway({CHAIN[3]: self.GOOD}, listed=CHAIN[:3])
        gateway.answers.update({name: self.title.TitlePassError("down", reason_code="provider_unreachable")
                                for name in CHAIN[:3]})
        result = self.run_title(gateway)
        self.assertEqual(result["evidence"]["model"], "automaticai/free")

    def test_an_unreadable_catalog_does_not_block_the_chain(self):
        class NoCatalog(FakeGateway):
            def __call__(self, url, *, key, model, timeout, payload=None):
                if payload is None:
                    raise self.owner.title.TitlePassError("no /models", reason_code="provider_http_error")
                return super().__call__(url, key=key, model=model, timeout=timeout, payload=payload)

        gateway = NoCatalog({CHAIN[0]: self.GOOD})
        gateway.owner = self
        self.assertEqual(self.run_title(gateway)["evidence"]["model"], CHAIN[0])

    def test_whole_chain_failure_reports_the_primarys_class_with_every_attempt(self):
        mixed = FakeGateway({
            CHAIN[0]: self.title._http_failure(_http_error(500), url="u", model=CHAIN[0]),
            CHAIN[1]: self.title._http_failure(_http_error(429), url="u", model=CHAIN[1]),
            CHAIN[2]: self.title.TitlePassError("slow", reason_code="timeout"),
            CHAIN[3]: self.title.TitlePassError("junk", reason_code="provider_bad_response")})
        with self.assertRaises(self.title.ProviderError) as caught:
            self.run_title(mixed)
        self.assertEqual(caught.exception.reason_code, "provider_http_error")
        for name in CHAIN:
            self.assertIn(name, str(caught.exception))

    def test_a_chain_rate_limited_end_to_end_says_so(self):
        limited = FakeGateway({name: self.title._http_failure(_http_error(429), url="u", model=name)
                               for name in CHAIN})
        with self.assertRaises(self.title.ProviderError) as caught:
            self.run_title(limited)
        self.assertEqual(caught.exception.reason_code, "provider_rate_limited")

    def test_a_failed_chain_leaves_the_reason_code_first_on_stderr(self):
        import sys
        limited = FakeGateway({name: self.title._http_failure(_http_error(429), url="u", model=name)
                               for name in CHAIN})
        with tempfile.TemporaryDirectory() as directory:
            md = self.note(directory)
            stderr = io.StringIO()
            with patch.object(self.title, "resolve_api_key", return_value="k"), \
                    patch.object(self.title, "_api_request", side_effect=limited), \
                    patch.object(sys, "argv", ["title-slug", str(md)]), patch.object(sys, "stderr", stderr):
                self.assertEqual(self.title.main(), 1)
        lines = stderr.getvalue().splitlines()
        self.assertEqual(lines[0], "reason_code=provider_rate_limited")
        self.assertIn("automaticai/free=provider_rate_limited/429", lines[1])

    def test_an_explicit_empty_fallback_list_means_a_single_model(self):
        with patch.dict(os.environ, {"WAX_TITLE_MODEL_FALLBACKS": ""}):
            self.assertEqual(self.title.model_chain(), [CHAIN[0]])
        with patch.dict(os.environ, {"WAX_TITLE_MODEL": "bogus/none"}):
            # A changed primary keeps the default fallbacks (the live fallback proof uses this).
            self.assertEqual(self.title.model_chain(), ["bogus/none", *CHAIN[1:]])

    def test_classification_falls_through_and_names_the_server(self):
        classification = load_script(PASSES_DIR / "bin" / "classification")
        gateway = FakeGateway({CHAIN[0]: classification.provider._http_failure(
            _http_error(429), url="u", model=CHAIN[0]),
            CHAIN[1]: _chat_answer({"classification": "meeting"})})
        with patch.object(classification.provider, "resolve_api_key", return_value="k"), \
                patch.object(classification.provider, "_api_request", side_effect=gateway):
            metadata, evidence = classification.enrich_document("Two people discussing a plan.")
        self.assertEqual(metadata, {"classification": "meeting"})
        self.assertEqual(evidence["model"], CHAIN[1])
        self.assertEqual(evidence["fallbacks"][0]["reason_code"], "provider_rate_limited")

    def test_ticketable_falls_through_and_names_the_server(self):
        from wax import provider
        ticketable = load_script(PASSES_DIR / "bin" / "ticketable")
        calls = []

        def chat(**kwargs):
            calls.append(kwargs["model"])
            if kwargs["model"] == CHAIN[0]:
                raise provider.ProviderError("HTTP 429", "provider_rate_limited", status=429)
            if kwargs["model"] == CHAIN[1]:
                raise provider.ProviderError("HTTP 503", "provider_http_error", status=503)
            return {"tickets": [{"project_id": "bb", "description": "Add the schema"}]}

        with patch.dict(os.environ, {"WAX_TICKETS_MODEL": "", "WAX_TICKETS_MODEL_FALLBACKS": ""}), \
                patch.object(provider, "resolve_key", return_value="k"), \
                patch.object(provider, "chat_json", side_effect=chat):
            os.environ.pop("WAX_TICKETS_MODEL_FALLBACKS")
            raw, windows, truncated, evidence = ticketable.extract(
                "Please add the schema.", fm={}, ids=["bb"], projects=[])
        self.assertEqual(calls, CHAIN[:3])
        self.assertEqual(raw, [{"project_id": "bb", "description": "Add the schema"}])
        self.assertEqual(evidence["model"], CHAIN[2])
        self.assertEqual([f["reason_code"] for f in evidence["fallbacks"]],
                         ["provider_rate_limited", "provider_http_error"])

    def test_ticketable_no_api_key_asks_no_model(self):
        from wax import provider
        ticketable = load_script(PASSES_DIR / "bin" / "ticketable")
        with patch.object(provider, "resolve_key", side_effect=provider.ProviderError("no key", "no_api_key")), \
                patch.object(provider, "chat_json") as chat:
            with self.assertRaises(provider.ProviderError) as caught:
                ticketable.extract("Please add the schema.", fm={}, ids=["bb"], projects=[])
        self.assertEqual(caught.exception.reason_code, "no_api_key")
        chat.assert_not_called()

    def test_the_403_scope_marker_is_recognised(self):
        from wax import provider
        err = provider._http_error(_http_error(
            403, '{"error":{"message":"This token has no access to model automaticai/x (request id: 1)"}}'),
            "u", "k")
        self.assertEqual((err.reason_code, err.detail_code, err.status),
                         ("provider_auth_rejected", "model_out_of_scope", 403))
        self.assertTrue(provider.falls_through(err))
        plain = provider._http_error(_http_error(403, "forbidden"), "u", "k")
        self.assertFalse(provider.falls_through(plain))


class JevRoutingTest(unittest.TestCase):
    """Jev (project-extraction) goes through the fleet Jev ingress, never OpenRouter."""

    def test_no_pass_yaml_env_reaches_openrouter(self):
        # Every pass, chat or not: an env value is what actually runs.
        for path in sorted(PASSES_DIR.glob("*.yaml")):
            env = (yaml.safe_load(path.read_text()) or {}).get("env") or {}
            self.assertNotIn("OPENROUTER_API_KEY", env, path.name)
            for key, value in env.items():
                with self.subTest(pass_=path.stem, var=key):
                    self.assertEqual(_forbidden(str(value)), "", f"{path.name} {key}={value}")

    def test_project_extraction_yaml_targets_the_ingress(self):
        env = yaml.safe_load((PASSES_DIR / "project-extraction.yaml").read_text())["env"]
        self.assertEqual(env["WAX_JEV_URL"], JEV_INGRESS)
        # OpenRouter's `typesafe/jev-*` spelling 400s on System One.
        self.assertFalse(str(env["WAX_JEV_MODEL"]).startswith("typesafe/"), env["WAX_JEV_MODEL"])
        self.assertTrue(str(env["WAX_JEV_API_KEY_OP"]).startswith("op://"))

    def test_jev_defaults_never_reach_openrouter(self):
        sources = {
            "wax/jev.py": COMPONENT_ROOT / "src" / "wax" / "jev.py",
            "bin/project-extraction": PASSES_DIR / "bin" / "project-extraction",
            "bin/wax": COMPONENT_ROOT / "bin" / "wax",
        }
        for label, path in sources.items():
            source = path.read_text()
            with self.subTest(source=label):
                self.assertNotIn("openrouter.ai/api/alpha/decisions", source)
                self.assertNotIn("openrouter.ai/api/v1/key", source)
                for value in DEFAULT_CONST.findall(source):
                    self.assertEqual(_forbidden(value), "", f"{label}: {value}")
                    self.assertFalse(value.startswith("typesafe/"), f"{label}: {value}")
        from wax import jev
        self.assertEqual(jev.DEFAULT_URL, JEV_INGRESS)
        self.assertNotIn("openrouter", jev.DEFAULT_MODEL.lower())
        # The decide() call site must not send OpenRouter attribution headers.
        self.assertNotIn("HTTP-Referer", sources["wax/jev.py"].read_text())


if __name__ == "__main__":
    unittest.main()
