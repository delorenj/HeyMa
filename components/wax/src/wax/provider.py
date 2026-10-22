"""OpenAI-compatible provider plumbing for the enrichment passes built after title-slug.

title-slug and classification keep their own copies of these helpers (their
tests patch them by module attribute). Everything here exists because of a
specific way those copies hurt once a second provider arrived:

- A key in `$WAX_TITLE_API_KEY` wins for EVERY caller of that resolver, so a
  Jev pass reusing it would silently send the gateway token to the Jev ingress.
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

import contextlib
import http.client
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, NamedTuple, Optional

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
# The gateway's 403 for a model outside the token's scope ("This token has no
# access to model X"). A 403 without this marker is a statement about the key.
_MODEL_OUT_OF_SCOPE = re.compile(r"token has no access to model|no access to model")
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
                 status: Optional[int] = None, detail_code: Optional[str] = None,
                 route: Optional[str] = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.status = status
        self.detail_code = detail_code
        # The gateway's X-AutomaticAI-Route on a failed request: which route a
        # router name (auto/fast, free) actually picked before it failed.
        self.route = route


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
    if _MODEL_OUT_OF_SCOPE.search(lowered):
        return "model_out_of_scope"
    return None


def gateway_route(headers: Any) -> Optional[str]:
    """X-AutomaticAI-Route from a response's headers (any case), or None."""
    if not headers:
        return None
    try:
        items = headers.items()
    except AttributeError:
        return None
    for name, value in items:
        if str(name).lower() == "x-automaticai-route" and str(value).strip():
            return str(value).strip()[:120]
    return None


def _http_error(exc: urllib.error.HTTPError, url: str, key: str) -> ProviderError:
    status = exc.code
    route = gateway_route(exc.headers)
    try:
        body = exc.read(_ERROR_BODY_LIMIT).decode("utf-8", "replace")
    except (OSError, ValueError, http.client.HTTPException):
        # A drained error body is still a real HTTP failure; losing the body
        # must never turn into losing the classification.
        body = ""
    error = _classify_http(status, body, url, key, exc.headers)
    error.route = route
    return error


def _classify_http(status: int, body: str, url: str, key: str, headers: Any) -> ProviderError:
    detail_code = _marker(body)
    where = f"HTTP {status} from {url}" + (f" ({detail_code})" if detail_code else "")
    if status == 403 and detail_code == "model_out_of_scope":
        return ProviderError(f"{where}: the model is outside the key's scope",
                             "provider_auth_rejected", status=status, detail_code=detail_code)
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
        retry_after = (headers.get("Retry-After") or "").strip() if headers else ""
        hint = f"; retry after {retry_after}s" if retry_after.isdigit() else ""
        return ProviderError(f"{where}: rate limited{hint}", "provider_rate_limited",
                             status=status, detail_code=detail_code)
    if status == 400:
        return ProviderError(f"{where}: the provider rejected the request", "provider_bad_request",
                             status=status, detail_code=detail_code)
    return ProviderError(where, "provider_http_error", status=status, detail_code=detail_code)


def transport_error(exc: BaseException, url: str, timeout: Optional[float]) -> ProviderError:
    """A failure below the HTTP status line, as a reason code the chain falls through on.

    http.client.HTTPException is NOT an OSError: a body cut short
    (IncompleteRead) or a garbled status line (BadStatusLine) escaped every
    `except OSError` and killed the pass with a traceback whose first stderr
    line was no `reason_code=`. A response that started and broke off is the
    provider's bad answer; a peer that hung up before answering is unreachable.
    """
    within = f" within {timeout:g}s" if timeout is not None else ""
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        if isinstance(exc.reason, TimeoutError):
            return ProviderError(f"{url} did not answer{within}", "timeout")
        return ProviderError(f"cannot reach {url}: {exc.reason}", "provider_unreachable")
    if isinstance(exc, TimeoutError):
        return ProviderError(f"{url} did not answer{within}", "timeout")
    if isinstance(exc, http.client.IncompleteRead):
        expected = f"{exc.expected} more expected" if exc.expected is not None else "more expected"
        return ProviderError(f"{url} cut its response short ({len(exc.partial or b'')} bytes read, "
                             f"{expected})", "provider_bad_response", detail_code="truncated_response")
    if isinstance(exc, http.client.RemoteDisconnected):
        return ProviderError(f"{url} closed the connection without answering", "provider_unreachable")
    if isinstance(exc, http.client.HTTPException):
        return ProviderError(f"{url} answered with a malformed HTTP response ({type(exc).__name__})",
                             "provider_bad_response", detail_code="malformed_response")
    return ProviderError(f"cannot reach {url}: {type(exc).__name__}: {exc}", "provider_unreachable")


def post_json(url: str, payload: dict[str, Any], *, key: str, timeout: float,
              headers: Optional[dict[str, str]] = None,
              user_agent: str = USER_AGENT,
              response_headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """POST one JSON object and return the JSON object the provider answers with.

    `response_headers`, when given, is filled with the success response's
    headers (a provider may carry its request id there rather than in the body).
    """
    return _request_json(url, payload, key=key, timeout=timeout, headers=headers,
                         user_agent=user_agent, response_headers=response_headers)


def get_json(url: str, *, key: str, timeout: float,
             user_agent: str = USER_AGENT) -> dict[str, Any]:
    """GET one JSON object, with the same error classification as post_json."""
    return _request_json(url, None, key=key, timeout=timeout, headers=None, user_agent=user_agent)


def list_model_ids(base: str, key: str, *, timeout: float = 10.0) -> Optional[set[str]]:
    """The model ids this key can see at `base`, or None when the catalog is unreadable.

    None is not evidence that a model is gone: not every OpenAI-compatible
    provider serves /models, so an unreadable catalog never vetoes a member.
    """
    try:
        payload = get_json(base.rstrip("/") + "/models", key=key, timeout=timeout)
    except ProviderError as exc:
        note(f"note: model catalog unreadable ({exc.reason_code}); chain members are not pre-checked")
        return None
    entries = payload.get("data")
    ids = {str(e.get("id")) for e in entries or [] if isinstance(e, dict) and e.get("id")}
    if not ids:
        note("note: the model catalog was empty; chain members are not pre-checked")
        return None
    return ids


# Router family names the gateway grants per token but never lists in /v1/models
# (observed 2026-10-22: a token scoped to `automaticai/free` does not see it in
# the listing). A catalog check must not read that absence as "not served".
VIRTUAL_MODELS = frozenset({"automaticai/free"})


def is_listed(model: str, ids: set[str]) -> bool:
    """OpenRouter routing suffixes (:nitro, :floor) never appear in a catalog."""
    return model in VIRTUAL_MODELS or model in ids or model.split(":", 1)[0] in ids


def _request_json(url: str, payload: Optional[dict[str, Any]], *, key: str, timeout: float,
                  headers: Optional[dict[str, str]], user_agent: str,
                  response_headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
    request_headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": user_agent,
    }
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    try:
        request = urllib.request.Request(
            url, data=None if payload is None else json.dumps(payload).encode("utf-8"),
            headers=request_headers, method="GET" if payload is None else "POST")
    except ValueError as exc:
        raise ProviderError(f"invalid provider URL {url!r}: {exc}", "run_error") from None
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read()
            if response_headers is not None:
                response_headers.update(response.headers.items())
    except urllib.error.HTTPError as exc:
        # MUST precede URLError, which it subclasses: catching the parent first
        # is precisely what once discarded the status that named a dead model.
        raise _http_error(exc, url, key) from None
    except (OSError, http.client.HTTPException) as exc:
        # URLError is an OSError; HTTPException (IncompleteRead, BadStatusLine)
        # is not, and must still become a reason code, never a traceback.
        raise transport_error(exc, url, timeout) from None
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
              temperature: float = 0.1, headers: Optional[dict[str, str]] = None,
              provenance: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """One strict json_schema chat completion, returned as the parsed object.

    `provenance`, when given, is filled with what the gateway says served the
    request (served_by/route/auto, see provenance_of). An Anthropic server is
    refused here, before its content is even parsed.
    """
    url = base.rstrip("/") + "/chat/completions"
    answer_headers: dict[str, str] = {}
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
    }, key=key, timeout=timeout, headers=headers, response_headers=answer_headers)
    served = provenance_of(response, answer_headers)
    refuse_anthropic(model, served)
    if provenance is not None:
        provenance.update(served)
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


# -- model fallback chains ----------------------------------------------------
#
# A chat pass names an ordered list of models: the primary, then fallbacks.
# 2026-10-22: the gateway's Hyper channel answered 503 "No available channel"
# while the free family and the subscription fast tier were healthy; a pass
# with one model name has nowhere to go, and its retry budget burns on the
# same dead route. The chain moves on only when the failure is about THIS
# model or THIS provider, never when it is about the caller.

# Reason codes that are about the route, not about us: try the next member.
FALLTHROUGH_REASONS = frozenset({
    "provider_rate_limited",    # 429
    "provider_quota_exceeded",  # 402
    "provider_http_error",      # 5xx and any other status
    "provider_unreachable",
    "timeout",
    "missing_model",            # a name the catalog does not serve
    "model_not_found",
    "provider_bad_request",     # 400: e.g. a member that rejects the temperature
    "provider_bad_response",    # unparseable or empty content
})


def split_models(raw: str) -> list[str]:
    """A comma-separated model list, in order, without blanks or repeats."""
    out: list[str] = []
    for part in (raw or "").split(","):
        name = part.strip()
        if name and name not in out:
            out.append(name)
    return out


def model_chain(primary_var: str, fallbacks_var: str, *, default_primary: str,
                default_fallbacks: str) -> list[str]:
    """[primary, *fallbacks] from the pass env, each falling back to its default.

    An explicitly empty fallbacks variable means "no fallbacks": a deliberate
    single-model pass (the fallback-path proof also relies on setting a bad
    primary and keeping the defaults).
    """
    primary = os.environ.get(primary_var, "").strip() or default_primary
    raw = os.environ.get(fallbacks_var)
    fallbacks = split_models(default_fallbacks if raw is None else raw)
    return split_models(",".join([primary, *fallbacks]))


def falls_through(exc: BaseException) -> bool:
    """Whether the next chain member should be tried after this failure."""
    code = getattr(exc, "reason_code", "")
    if code in FALLTHROUGH_REASONS:
        return True
    # A 403 that only says the model is outside the token's scope is about the
    # model. Any other 401/403 is about the key, and every member shares it.
    return code == "provider_auth_rejected" and getattr(exc, "detail_code", None) == "model_out_of_scope"


class ChainFailed(ProviderError):
    """Every chain member failed (or one failed in a way no member can fix).

    reason_code is the class of the first member that was actually ASKED: a
    member skipped without a request (unlisted, out of budget) says nothing
    about the providers, so it never names the failure. A chain rate limited
    end to end says so; missing_model only when every member was skipped.
    `attempts` lists every member, tried or skipped.
    """

    def __init__(self, attempts: list[dict[str, Any]], *, reason_code: str,
                 status: Optional[int] = None, detail_code: Optional[str] = None) -> None:
        super().__init__("no model in the chain answered: " + describe_attempts(attempts),
                         reason_code, status=status, detail_code=detail_code)
        self.attempts = attempts


def describe_attempts(attempts: list[dict[str, Any]]) -> str:
    """One short line per member: `model=reason/status[->route]`; never a body."""
    parts = []
    for item in attempts:
        status = f"/{item['status']}" if item.get("status") else ""
        route = f"->{item['route']}" if item.get("route") and item["route"] != item["model"] else ""
        parts.append(f"{item['model']}={item['reason_code']}{status}{route}")
    return "; ".join(parts)


# -- deadlines ----------------------------------------------------------------
#
# A socket timeout is not a deadline. urllib applies it to each connect and
# each recv, so a gateway trickling a byte at a time never trips it, and a
# chain of N members can spend N x timeout. The runner kills a pass at its
# `timeout_s` and keeps nothing the pass printed, so a chain cut off there
# loses exactly the per-member evidence that says which route failed. A chat
# pass therefore runs its chain against ONE monotonic deadline, taken at start
# from the timeout the runner enforces (WAX_PASS_TIMEOUT_S) minus a reserve:
# each request gets min(its cap, what is left minus a margin), a wall-clock
# limit covers what a socket timeout cannot see, and a member with too little
# left is skipped and recorded rather than started and killed.

PASS_TIMEOUT_ENV = "WAX_PASS_TIMEOUT_S"  # set by wax.passes.run to the timeout_s it enforces
RESERVE_S = 10.0         # between the chain deadline and the runner's kill: report and exit
REQUEST_MARGIN_S = 1.0   # a request's own timeout ends this long before the deadline
MIN_REQUEST_S = 2.0      # less than this left: skip the member, do not start it
HARD_GRACE_S = 0.5       # the wall-clock limit fires this long after the socket timeout


class DeadlineExceeded(Exception):
    """The wall-clock limit fired. Deliberately not an OSError or ProviderError,
    so no transport handler between the signal and run_chain can swallow it."""


class Deadline:
    """A budget on the monotonic clock, shared by every member of a chain."""

    def __init__(self, seconds: float, *, clock=time.monotonic) -> None:
        self._clock = clock
        self.budget = max(0.0, float(seconds))
        self.expires = clock() + self.budget

    def remaining(self) -> float:
        return max(0.0, self.expires - self._clock())

    def request_timeout(self, cap: Optional[float]) -> float:
        """The timeout for the next request: its cap, never past the deadline."""
        left = self.remaining() - REQUEST_MARGIN_S
        return max(0.0, left if cap is None else min(float(cap), left))

    def share(self, parts: int) -> "Deadline":
        """A sub-deadline holding 1/parts of what is left (one of N windows)."""
        return Deadline(self.remaining() / max(1, parts), clock=self._clock)


def pass_deadline(default_timeout_s: float) -> Deadline:
    """The chain deadline for this pass run: the runner's timeout minus RESERVE_S.

    WAX_PASS_TIMEOUT_S is what the runner will actually enforce (a compiled
    pipeline's frozen timeout_s included); `default_timeout_s` mirrors the
    pass YAML for a bare-shell run.
    """
    timeout = default_timeout_s
    raw = os.environ.get(PASS_TIMEOUT_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            timeout = value
        else:
            note(f"note: {PASS_TIMEOUT_ENV}={raw!r} is not a positive number; budgeting {default_timeout_s:g}s")
    return Deadline(max(0.0, timeout - RESERVE_S))


@contextlib.contextmanager
def wall_clock_limit(seconds: Optional[float]):
    """Raise DeadlineExceeded after `seconds` of wall time, whatever is blocking.

    SIGALRM interrupts a blocking recv/poll and its handler's exception
    propagates (PEP 475 retries only when the handler returns). Main thread
    only, as signal handlers are; elsewhere this degrades to the socket
    timeout alone. A name lookup blocked inside getaddrinfo is not interrupted.
    """
    usable = (seconds is not None and seconds > 0 and hasattr(signal, "setitimer")
              and threading.current_thread() is threading.main_thread())
    if not usable:
        yield
        return

    def _fire(_signum, _frame):
        raise DeadlineExceeded(f"no complete answer within {seconds:g}s of wall time")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


# -- provenance -----------------------------------------------------------------
#
# The chain member is what Wax ASKED for; a router name (auto/fast, free) says
# nothing about who answered. The gateway says it in the response body's
# `model` and in X-AutomaticAI-Route / X-AutomaticAI-Auto (X-AutomaticAI-Free
# for the free router). Operator order 2026-10-22: no Anthropic model serves
# Wax. A scope or router regression must fail the pass loudly, never serve one
# silently, so an Anthropic server is refused with its own reason code, and it
# does not fall through: the next member succeeding would hide the regression.

ANTHROPIC_RE = re.compile(r"claude|sonnet|haiku|opus|fable|anthropic", re.IGNORECASE)
ANTHROPIC_SERVED = "anthropic_model_served"
ANTHROPIC_REFUSED = "anthropic_model_refused"


def provenance_of(payload: Any, headers: Any) -> dict[str, str]:
    """served_by (the response's `model`), route and auto decision, where present."""
    out: dict[str, str] = {}
    served = payload.get("model") if isinstance(payload, dict) else None
    if isinstance(served, str) and served.strip():
        out["served_by"] = served.strip()[:120]
    lowered: dict[str, str] = {}
    try:
        lowered = {str(k).lower(): str(v).strip() for k, v in (headers or {}).items()}
    except AttributeError:
        pass
    if lowered.get("x-automaticai-route"):
        out["route"] = lowered["x-automaticai-route"][:120]
    decision = lowered.get("x-automaticai-auto") or lowered.get("x-automaticai-free")
    if decision:
        out["auto"] = decision[:240]
    return out


def anthropic_in(provenance: dict[str, Any]) -> Optional[str]:
    """`served_by=<x>` / `route=<x>` when either names an Anthropic model."""
    for field in ("served_by", "route"):
        value = provenance.get(field)
        if isinstance(value, str) and ANTHROPIC_RE.search(value):
            return f"{field}={value}"
    return None


def refuse_anthropic(model: str, provenance: dict[str, Any]) -> None:
    banned = anthropic_in(provenance)
    if banned:
        raise ProviderError(
            f"{model} was served by an Anthropic model ({banned}); refused, operator order "
            "2026-10-22. The token scope or a router regressed: run deploy/provision-inference",
            ANTHROPIC_SERVED)


class Answer:
    """A chain member's value plus what the gateway says served it."""

    __slots__ = ("value", "provenance")

    def __init__(self, value: Any, provenance: Optional[dict[str, str]] = None) -> None:
        self.value = value
        self.provenance = dict(provenance or {})


class ChainResult(NamedTuple):
    value: Any
    model: str                      # the chain member that answered
    attempts: list[dict[str, Any]]  # every member before it, tried or skipped
    provenance: dict[str, str]      # served_by / route / auto, as the gateway reported


def _attempt(model: str, exc: ProviderError) -> dict[str, Any]:
    route = getattr(exc, "route", None)
    return {"model": model, "reason_code": exc.reason_code,
            **({"status": exc.status} if exc.status else {}),
            **({"route": route} if route else {}),
            "detail": str(exc)[:200]}


def run_chain(models: list[str], call, *, listed=None, deadline: Optional[Deadline] = None,
              request_timeout: Optional[float] = None) -> ChainResult:
    """Run `call(model, timeout)` down `models`; return the first answer as a ChainResult.

    `call` may return a plain value or an Answer carrying the gateway's
    provenance. `listed(model)` False skips a member without a request
    (missing_model). With a `deadline`, each request's timeout is
    min(request_timeout, remaining - margin) under a wall-clock limit, and a
    member with less than MIN_REQUEST_S left is skipped as deadline_exceeded,
    so the chain always ends, with its evidence, before the runner's kill.
    A failure that does not fall through (no_api_key, a rejected key, an
    Anthropic server) stops the chain at once.
    """
    attempts: list[dict[str, Any]] = []
    for index, model in enumerate(models):
        if ANTHROPIC_RE.search(model):
            attempts.append({"model": model, "reason_code": ANTHROPIC_REFUSED, "skipped": True,
                             "detail": "an Anthropic chain member is never asked (operator order 2026-10-22)"})
            note(f"note: refused chain member {model}: Anthropic models never serve Wax")
            continue
        if listed is not None and not listed(model):
            attempts.append({"model": model, "reason_code": "missing_model", "skipped": True,
                             "detail": "not listed for this token"})
            continue
        timeout = request_timeout
        if deadline is not None:
            timeout = deadline.request_timeout(request_timeout)
            if timeout < MIN_REQUEST_S:
                left = deadline.remaining()
                for rest in models[index:]:
                    attempts.append({"model": rest, "reason_code": "deadline_exceeded", "skipped": True,
                                     "detail": f"{left:.1f}s left of the {deadline.budget:g}s chain budget"})
                break
        try:
            with wall_clock_limit(None if deadline is None else timeout + HARD_GRACE_S):
                answer = call(model, timeout)
        except DeadlineExceeded:
            attempts.append({"model": model, "reason_code": "timeout",
                             "detail": f"no complete answer within {timeout:g}s (chain deadline)"})
            continue
        except ProviderError as exc:
            attempts.append(_attempt(model, exc))
            if not falls_through(exc):
                raise ChainFailed(attempts, reason_code=exc.reason_code, status=exc.status,
                                  detail_code=exc.detail_code) from exc
            continue
        if isinstance(answer, Answer):
            value, provenance = answer.value, answer.provenance
        else:
            value, provenance = answer, {}
        banned = anthropic_in(provenance)
        if banned:
            attempts.append({"model": model, "reason_code": ANTHROPIC_SERVED, **provenance,
                             "detail": f"refused: {banned}"})
            raise ChainFailed(attempts, reason_code=ANTHROPIC_SERVED)
        return ChainResult(value, model, attempts, provenance)
    tried = [item for item in attempts if not item.get("skipped")]
    codes = [item["reason_code"] for item in tried]
    if not tried:
        skipped = {item["reason_code"] for item in attempts}
        reason = ("timeout" if "deadline_exceeded" in skipped
                  else ANTHROPIC_REFUSED if skipped == {ANTHROPIC_REFUSED} else "missing_model")
        raise ChainFailed(attempts, reason_code=reason)
    reason = "provider_rate_limited" if set(codes) == {"provider_rate_limited"} else codes[0]
    raise ChainFailed(attempts, reason_code=reason, status=tried[0].get("status"))


def evidence_for(models: list[str], served: str, attempts: list[dict[str, Any]],
                 provenance: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """The pass evidence: the member that answered, who actually served it, the
    chain, and why each earlier member did not."""
    return {"model": served, **(provenance or {}), "chain": models,
            "fallbacks": [{k: v for k, v in item.items() if k != "detail"} for item in attempts]}


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
