"""OpenAI-compatible provider plumbing for the enrichment passes built after title-slug.

title-slug and classification keep their own copies of these helpers (their
tests patch them by module attribute). Everything here exists because of a
specific way those copies hurt once a second provider arrived:

- A key in `$WAX_TITLE_API_KEY` wins for EVERY caller of that resolver, so a
  Jev pass reusing it would silently send the gateway token to OpenRouter.
  Each pass here names its own env var and its own op:// reference.
- "The model name appears in the error body" was the missing-model test. A
  Jev 400 echoes the whole request, model id included, so that test misreads
  a validation error as a deleted model. Status classes map to reason codes
  here; a body is only ever searched for a known marker.
- Provider bodies are not ours to print. An OpenRouter 400 carries the org id,
  and model output can quote the transcript. Messages carry the URL, the
  status and a marker, never a body and never content.
- A failed pass leaves `reason_code=<code>` as the FIRST stderr line. A bare
  nonzero exit is how a week of 404s was recorded as an indistinguishable
  "nonzero_exit".

Nothing in this module writes a file or logs a key.
"""

import http.client
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

USER_AGENT = "HeyMa-Wax/1.0"
# `op read` is a local IPC round-trip to an already-authenticated agent. If it
# has not answered in 5s something is wrong with op, not with the network.
OP_READ_TIMEOUT_S = 5.0
# An error body is only searched for markers, so there is no reason to buffer
# a large one.
_ERROR_BODY_LIMIT = 64 * 1024
# The runner's own pattern (passes._REASON_LINE): lowercase and underscores
# only. A code with a digit in it would not be recognised at all.
_REASON = re.compile(r"[a-z_]+")
_MODEL_MISSING = re.compile(r"model\b.{0,160}\b(?:does not exist|not found|is not a valid model)", re.DOTALL)

# Which reference produced each key resolved in this process. A 401 is always
# a statement about one specific credential, so the failure names it: the
# whole cost of the 2026-09-08 outage was a message that said "User not found"
# and never said whose key was not found. In-memory only, never printed.
_KEY_SOURCES: dict[str, str] = {}

# Non-fatal observations, held back so that on failure `reason_code=` is
# genuinely the first line of stderr.
_NOTES: list[str] = []


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is a failure, never followed with the key attached.

    urllib re-sends Authorization when it turns a redirected POST into a GET,
    to whatever host the Location names.
    """

    def redirect_request(self, *_args, **_kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class ProviderError(RuntimeError):
    """A failure carrying the reason code the runner classifies on.

    Provider calls raise it; passes raise it for their own failures too, so a
    pass has exactly one failure type to turn into `fail()`.
    """

    def __init__(self, message: str, reason_code: str = "run_error", *,
                 status: Optional[int] = None, detail_code: Optional[str] = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.status = status
        self.detail_code = detail_code


def note(message: str) -> None:
    _NOTES.append(message)


def flush_notes() -> None:
    for message in _NOTES:
        print(message, file=sys.stderr)
    _NOTES.clear()


def env_number(name: str, default: float, *, minimum: float = 0.0,
               maximum: Optional[float] = None) -> float:
    """A numeric setting from the pass env block, or its default with a note.

    A typo in a threshold must not become a threshold of 0.0 (accept every
    project) or crash the pass on an unrelated note.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        note(f"note: {name}={raw!r} is not a number; using {default:g}")
        return default
    if value < minimum or (maximum is not None and value > maximum):
        note(f"note: {name}={raw!r} is out of range; using {default:g}")
        return default
    return value


def resolve_key(*, env_var: str, op_ref: str, op_timeout: float = OP_READ_TIMEOUT_S) -> str:
    """A literal `$env_var` wins; otherwise `op read op_ref`; otherwise no_api_key.

    VERIFIED 2026-08-19: OP_SERVICE_ACCOUNT_TOKEN is present in waxd's environ,
    so `op read` succeeds unattended. Only the reference ever reaches a file.
    """
    direct = os.environ.get(env_var, "").strip() if env_var else ""
    if direct:
        _KEY_SOURCES[direct] = f"${env_var}"
        return direct
    if not op_ref:
        raise ProviderError(f"no API key: ${env_var} is unset and no op:// reference is configured",
                            "no_api_key")
    try:
        proc = subprocess.run(["op", "read", op_ref], capture_output=True, text=True,
                              timeout=op_timeout)
    except subprocess.TimeoutExpired:
        raise ProviderError(f"`op read {op_ref}` did not answer within {op_timeout:g}s",
                            "no_api_key") from None
    except OSError as exc:
        raise ProviderError(f"`op read {op_ref}` could not run: {exc}", "no_api_key") from None
    key = proc.stdout.strip() if proc.returncode == 0 else ""
    if not key:
        why = (proc.stderr or "").strip().splitlines()
        reason = why[-1][:200] if why else f"exit {proc.returncode}"
        raise ProviderError(f"{op_ref} did not resolve ({reason}) and ${env_var} is unset",
                            "no_api_key")
    _KEY_SOURCES[key] = op_ref
    return key


def _marker(body: str) -> Optional[str]:
    """Classify an error body by marker. The body itself never leaves here."""
    lowered = body.lower()
    if "max_tokens_exceeded" in lowered:
        return "max_tokens_exceeded"
    if "model_not_found" in lowered or _MODEL_MISSING.search(lowered):
        return "model_not_found"
    return None


def _http_error(exc: urllib.error.HTTPError, url: str, key: str) -> ProviderError:
    status = exc.code
    try:
        body = exc.read(_ERROR_BODY_LIMIT).decode("utf-8", "replace")
    except (OSError, ValueError, http.client.HTTPException):
        # A drained error body is still a real HTTP failure; losing the body
        # must never turn into losing the classification.
        body = ""
    detail_code = _marker(body)
    where = f"HTTP {status} from {url}" + (f" ({detail_code})" if detail_code else "")
    if status in (401, 403):
        # OpenRouter answers a management/provisioning key with the same
        # `401 User not found.` it gives a revoked one (wax title-slug lost a
        # day to that on 2026-09-08), so name the key and the one-line test.
        return ProviderError(
            f"{where}: the key from {_KEY_SOURCES.get(key, 'the caller')} was rejected; confirm it "
            f"is an inference key, not a management/provisioning key (is_provisioning_key=false)",
            "provider_auth_rejected", status=status, detail_code=detail_code)
    if status == 402:
        return ProviderError(f"{where}: the key's spend limit is exhausted",
                             "provider_quota_exceeded", status=status, detail_code=detail_code)
    if status == 429:
        retry_after = (exc.headers.get("Retry-After") or "").strip() if exc.headers else ""
        hint = f"; retry after {retry_after}s" if retry_after.isdigit() else ""
        return ProviderError(f"{where}: rate limited{hint}", "provider_rate_limited",
                             status=status, detail_code=detail_code)
    if status == 400:
        return ProviderError(f"{where}: the provider rejected the request", "provider_bad_request",
                             status=status, detail_code=detail_code)
    return ProviderError(where, "provider_http_error", status=status, detail_code=detail_code)


def post_json(url: str, payload: dict[str, Any], *, key: str, timeout: float,
              headers: Optional[dict[str, str]] = None,
              user_agent: str = USER_AGENT) -> dict[str, Any]:
    """POST one JSON object and return the JSON object the provider answers with."""
    request_headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": user_agent,
    }
    request_headers.update(headers or {})
    try:
        request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                         headers=request_headers, method="POST")
    except ValueError as exc:
        raise ProviderError(f"invalid provider URL {url!r}: {exc}", "run_error") from None
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # MUST precede URLError, which it subclasses: catching the parent first
        # is precisely what once discarded the status that named a dead model.
        raise _http_error(exc, url, key) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ProviderError(f"{url} did not answer within {timeout:g}s", "timeout") from None
        raise ProviderError(f"cannot reach {url}: {exc.reason}", "provider_unreachable") from None
    except TimeoutError:
        raise ProviderError(f"{url} did not answer within {timeout:g}s", "timeout") from None
    except (OSError, http.client.HTTPException) as exc:
        raise ProviderError(f"cannot reach {url}: {type(exc).__name__}: {exc}",
                            "provider_unreachable") from None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProviderError(f"{url} answered with a body that is not JSON ({len(raw)} bytes)",
                            "provider_bad_response") from None
    if not isinstance(value, dict):
        raise ProviderError(f"{url} answered with JSON that is not an object",
                            "provider_bad_response")
    return value


def parse_json_object(text: str) -> dict[str, Any]:
    """The JSON object in a model's content: bare, fenced, or between braces."""
    raw = (text or "").strip()
    candidates = [raw]
    fenced = re.search(r"```(?:json)?[ \t]*\n(.*?)\n[ \t]*```", raw, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidates.append(fenced.group(1).strip())
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start:end + 1])
    # Bare JSON is tried first: a description that itself contains a code
    # fence must not send the fence parser into the middle of a string.
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
        raise ProviderError("model returned JSON that is not an object", "provider_bad_response")
    raise ProviderError(f"model returned content that is not JSON ({len(raw)} chars)",
                        "provider_bad_response")


def chat_json(*, base: str, model: str, key: str, system: str, user: str,
              schema: dict[str, Any], schema_name: str, max_tokens: int, timeout: float,
              temperature: float = 0.1, headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """One strict json_schema chat completion, returned as the parsed object."""
    url = base.rstrip("/") + "/chat/completions"
    response = post_json(url, {
        "model": model,
        # Unset, OpenRouter prices the request against the model's full output
        # ceiling and refuses an affordable call on a key with a spend limit.
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": schema_name, "strict": True, "schema": schema}},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }, key=key, timeout=timeout, headers=headers)
    try:
        choice = response["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError):
        # Quota and upstream stops arrive as a 200 with an error envelope.
        # Only a short code token is repeated; the error message is a body.
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        code = str(error.get("code", ""))
        hint = f" (provider error code {code})" if re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", code) else ""
        raise ProviderError(f"{url} answered without message content{hint}",
                            "provider_bad_response") from None
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content
                          if isinstance(part, dict) and isinstance(part.get("text"), str))
    finish = choice.get("finish_reason") if isinstance(choice, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ProviderError(f"{model} returned empty content (finish_reason={finish})",
                            "provider_bad_response")
    try:
        return parse_json_object(content)
    except ProviderError:
        if finish == "length":
            raise ProviderError(f"{model} hit max_tokens={max_tokens} before finishing its JSON",
                                "provider_bad_response", detail_code="output_truncated") from None
        raise


def document_text(body: str) -> str:
    """The text a provider should judge: the body without Wax's header line.

    Transcripts open with `# Transcription: <audio file>`. The audio filename
    is not something the speaker said, and it differs per recording, so it is
    noise in a decision and a cache-buster in a prompt. A note without that
    header (an ordinary document) passes through unchanged.
    """
    text = (body or "").strip()
    first, _, rest = text.partition("\n")
    if first.startswith("# Transcription:"):
        return rest.strip()
    return text


def windows(text: str, size: int, overlap: int) -> list[str]:
    """Overlapping windows that together cover all of `text`.

    The overlap is what keeps a sentence cut at a boundary whole in at least
    one window.
    """
    if size <= overlap:
        raise ValueError("window size must exceed the overlap")
    if len(text) <= size:
        return [text]
    out, start = [], 0
    while True:
        out.append(text[start:start + size])
        if start + size >= len(text):
            return out
        start += size - overlap


def fail(reason_code: str, detail: str) -> int:
    """Report a failure the runner can classify; return the exit status."""
    code = reason_code if isinstance(reason_code, str) and _REASON.fullmatch(reason_code) else "run_error"
    print(f"reason_code={code}", file=sys.stderr)
    if code != reason_code:
        print(f"(unclassifiable reason code {reason_code!r})", file=sys.stderr)
    print(detail, file=sys.stderr)
    flush_notes()
    return 1


def emit_result(result: dict[str, Any]) -> None:
    """Print the result as ONE compact line; the runner takes the last JSON line.

    ASCII escapes keep the line decodable whatever locale the runner's pipe
    is read with; the parsed values are identical.
    """
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")), flush=True)
    flush_notes()
