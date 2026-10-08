"""TypeSafe's Jev decision model, callable from any pass.

Jev answers questions about a JSON state object: `noul` (P(true) for one
yes/no question), `choice` (a softmax over named options, so ONE winner) and
`score` (a position on an ordered scale). One request carries any number of
named questions; the binding limit is a ~32K-token total for state plus
questions (measured 2026-10-08), rejected as a 400 whose body says
`max_tokens_exceeded`.

The decisions API lives at OpenRouter, not behind the AutomaticAI gateway:
every gateway path for it 404s, and the gateway's own Jev classifier calls
OpenRouter directly too. Callers therefore hold a dedicated OpenRouter
inference key, which is the documented exception to "route through
api.automaticai.io".

Answers are validated before anyone trusts them. Jev passes degrade silently
by construction otherwise: a missing answer read as 0.0 is indistinguishable
from a confident "no", and a pass that turns that into an empty result is the
exact outage shape Wax has already lived through twice.
"""

import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from . import provider

DEFAULT_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.13"
# Warm calls take 0.3-0.7s whatever the question count; the documented (not
# reproduced) cold start is ~4s. 20s is generous without hiding an outage.
DEFAULT_TIMEOUT_S = 20.0
# One retry for a transport failure or a 5xx; a 4xx is never retried.
DEFAULT_ATTEMPTS = 2
RETRY_BACKOFF_S = 1.0
_QUESTION_TYPES = ("noul", "choice", "score")


def noul(instructions: str, true: str, false: str) -> dict[str, Any]:
    """A yes/no question; the answer is P(true) in [0, 1]."""
    return {"type": "noul", "instructions": instructions,
            "criteria": {"true": true, "false": false}}


def choice(instructions: str, options: Mapping[str, str]) -> dict[str, Any]:
    """Exactly one of `options` (name -> meaning). A softmax, never multi-label."""
    if not options:
        raise ValueError("a choice question needs at least one option")
    return {"type": "choice", "instructions": instructions, "criteria": dict(options)}


def score(instructions: str, criteria: list[str]) -> dict[str, Any]:
    """A position on the ordered scale `criteria`, from 0 to len(criteria) - 1."""
    if not criteria:
        raise ValueError("a score question needs at least one level")
    return {"type": "score", "instructions": instructions, "criteria": list(criteria)}


@dataclass(frozen=True)
class Decision:
    answers: dict[str, dict[str, Any]]
    # The dated build that answered (e.g. typesafe/jev-1.13-20260917). The
    # alias moves under you, so provenance records this, not the request.
    model: str
    cost: float
    request_id: str

    def noul(self, name: str) -> float:
        return float(self.answers[name]["noul"])


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _probability(value: Any) -> bool:
    return _number(value) and 0 <= value <= 1


def _malformed(message: str) -> provider.ProviderError:
    return provider.ProviderError(f"Jev answer is malformed: {message}", "provider_bad_response")


def _validate(questions: Mapping[str, dict[str, Any]], response: dict[str, Any]) -> dict[str, dict[str, Any]]:
    answers = response.get("answers")
    if not isinstance(answers, dict):
        raise _malformed("no answers object")
    out: dict[str, dict[str, Any]] = {}
    for name, question in questions.items():
        kind = question["type"]
        answer = answers.get(name)
        if not isinstance(answer, dict) or answer.get("type") != kind:
            raise _malformed(f"no {kind} answer for {name!r}")
        if kind == "noul" and not _probability(answer.get("noul")):
            raise _malformed(f"{name!r} noul is not a probability")
        if kind == "choice":
            options = question["criteria"]
            probabilities = answer.get("probabilities")
            if answer.get("choice") not in options:
                raise _malformed(f"{name!r} chose an option that was not offered")
            if (not isinstance(probabilities, dict)
                    or any(option not in options or not _probability(p) for option, p in probabilities.items())):
                raise _malformed(f"{name!r} probabilities are not probabilities over the options")
            if "confidence" in answer and not _probability(answer["confidence"]):
                raise _malformed(f"{name!r} confidence is not a probability")
        if kind == "score":
            value = answer.get("score")
            if not _number(value) or not 0 <= value <= len(question["criteria"]) - 1:
                raise _malformed(f"{name!r} score is off the {len(question['criteria'])}-level scale")
        out[name] = dict(answer)
    return out


def decide(state: dict[str, Any], questions: Mapping[str, dict[str, Any]], *, key: str,
           model: str = DEFAULT_MODEL, url: str = DEFAULT_URL, timeout: float = DEFAULT_TIMEOUT_S,
           title: str = "HeyMa Wax", post: Optional[Callable[..., dict[str, Any]]] = None,
           attempts: int = DEFAULT_ATTEMPTS) -> Decision:
    """Ask every question about `state` in one request and validate every answer.

    `post` defaults to provider.post_json, resolved per call so it can be
    replaced in tests. A request too large for Jev's token budget raises
    reason `jev_too_large`, which is the caller's cue to send less state.
    """
    if not questions or any(not isinstance(q, dict) or q.get("type") not in _QUESTION_TYPES
                            for q in questions.values()):
        raise provider.ProviderError("Jev needs at least one noul/choice/score question", "run_error")
    send = post or provider.post_json
    payload = {"model": model, "state": state, "questions": dict(questions)}
    # OpenRouter attributes spend and rate limits to these headers; without
    # them Wax is indistinguishable from every other holder of the key.
    headers = {"X-Title": title, "HTTP-Referer": "https://delo.sh/wax"}
    for attempt in range(1, max(1, attempts) + 1):
        try:
            response = send(url, payload, key=key, timeout=timeout, headers=headers)
            break
        except provider.ProviderError as exc:
            if exc.reason_code == "provider_bad_request" and exc.detail_code == "max_tokens_exceeded":
                raise provider.ProviderError(f"Jev's token budget was exceeded: {exc}", "jev_too_large",
                                             status=exc.status, detail_code=exc.detail_code) from None
            transient = (exc.reason_code in ("timeout", "provider_unreachable")
                         or (exc.reason_code == "provider_http_error" and (exc.status or 0) >= 500))
            if not transient or attempt >= attempts:
                raise
            status = f", HTTP {exc.status}" if exc.status else ""
            provider.note(f"note: Jev attempt {attempt} failed ({exc.reason_code}{status}); retrying")
            time.sleep(RETRY_BACKOFF_S * attempt)
    answers = _validate(questions, response)
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    cost = usage.get("cost")
    echoed = response.get("model")
    request_id = response.get("id")
    return Decision(answers=answers,
                    model=echoed if isinstance(echoed, str) and echoed else model,
                    cost=float(cost) if _number(cost) and cost >= 0 else 0.0,
                    request_id=request_id if isinstance(request_id, str) else "")
