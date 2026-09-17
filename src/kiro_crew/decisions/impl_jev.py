"""TypeSafe System One (Jev) over HTTP.

Wire format taken from ``https://docs.typesafe.ai/api``, not inferred:

    POST https://api.typesafe.ai/v1/systemone
    Authorization: Bearer <API_KEY>
    {"state": ..., "model": "jev-latest", "questions": {id: Question}}

    -> {"model": ..., "answers": {id: Answer}, "usage": {...}}

Every field name lives in :func:`_to_wire` / :func:`_from_wire` and nowhere
else, so a schema change upstream is a one-function edit rather than a hunt.
That containment is the reason the two functions are written out rather than
inlined into :meth:`JevOracle.ask`.

The two mappings that are NOT one-to-one
---------------------------------------
* ``Choice.options`` -> ``criteria`` is a MAP of option to rubric text, and the
  rubric is optional per option (``null``). We have options without rubrics, so
  every value is ``None``. Sending a list would be rejected: for a Choice the
  API requires a map.
* ``Noul`` answers carry NO ``confidence`` field -- only ``noul``. That is the
  API's shape, not an omission here, and it is why ``Answer.confidence`` is
  optional. The yes-probability is put in both ``value`` and ``p`` because for
  this type they are the same quantity.

Errors are raised, never swallowed
----------------------------------
429, 5xx, 529, a body that is not JSON, and a body missing ``answers`` all
raise. The gate turns each into ``None`` plus one error identifier, so the
retry/backoff question stays where the policy lives (``provider.timeout_ms``, and
for now no retry at all) instead of being answered twice. No retry is deliberate:
the gate's budget is a single sub-second deadline, so a backoff could only spend
it and then fail anyway.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.decisions.types import Answer, Answers, Choice, Noul, Question

logger = logging.getLogger(__name__)

#: Ceiling on a provider response body. An answer set for the question counts
#: this seam sends is kilobytes; anything approaching this is a broken or hostile
#: provider, and ``resp.text()`` would otherwise read to EOF and allocate all of
#: it. Overflow is refused rather than truncated: a half-read JSON body is not a
#: smaller answer, it is an unparseable one, and the row should say so.
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

#: Chunk size for the bounded read above. Only bounds how much overshoot is held
#: in memory before the refusal fires, so it wants to be small next to the cap.
_RESPONSE_CHUNK_BYTES = 64 * 1024

#: Endpoint used when the provider config carries none.
_DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"

#: Model used when the provider config carries none.
_DEFAULT_MODEL = "jev-latest"

#: ``api_key`` values with this prefix name a vault entry rather than carrying a
#: key. A plaintext value is still accepted (an operator mid-migration), but the
#: reference form is what the plan ships and what config help documents.
_SECRET_PREFIX = "secret://"


class JevProtocolError(RuntimeError):
    """The provider answered, but not in a shape this module can read."""


class JevHttpError(RuntimeError):
    """The provider answered with a non-2xx status.

    Carries ``status`` so a caller can tell a 429 from a 401 without parsing the
    message -- the gate logs the message, but a future retry policy needs the
    number.
    """

    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(f"HTTP {status}{': ' + detail if detail else ''}")
        self.status = status


def resolve_api_key(raw: str, *, endpoint: str = _DEFAULT_ENDPOINT) -> str:
    """The bearer token for *raw*, resolving a ``secret://NAME`` reference.

    Returns ``""`` when there is nothing to send: an empty setting, or a
    reference to an entry the vault does not hold. The caller raises on empty
    rather than sending an ``Authorization: Bearer`` with no token, which the
    provider would answer 401 to -- a real 401 must not be indistinguishable from
    never having configured a key.

    Vault errors also resolve to ``""``: the distinction that matters downstream
    is "no usable key", and the vault's own exception text can name a path. A
    vault reference is resolved only for the default endpoint; a custom endpoint
    must use a literal key already in the operator's config.
    """
    value = (raw or "").strip()
    if not value:
        return ""
    if not value.startswith(_SECRET_PREFIX):
        return value
    if endpoint != _DEFAULT_ENDPOINT:
        return ""
    name = value[len(_SECRET_PREFIX) :].strip()
    if not name:
        return ""
    # Local imports: the vault pulls in cryptography, and a config where the
    # point is off must not pay for it.
    from kiro_crew.config.paths import config_dir
    from kiro_crew.secrets.vault import SecretVault

    try:
        secret = SecretVault(config_dir()).get(name)
    except Exception as exc:
        # The exception CLASS only -- not its message (the vault's text can
        # carry a filesystem path), not the resolved value, and not the entry
        # name (CodeQL reads it as sensitive, and Semgrep's
        # logger-credential-disclosure refuses a credential-shaped word in a
        # logger call at all). The class was always the whole diagnostic: it
        # separates "no vault" from "wrong passphrase" from "absent entry", and
        # this module performs exactly one lookup so the subject is unambiguous.
        logger.warning(
            "decisions: vault lookup for the provider failed: %s",
            type(exc).__name__,
        )
        return ""
    return secret.reveal() if secret is not None else ""


def _to_wire(state: dict | str, model: str, questions: list[Question]) -> dict[str, Any]:
    """Build the request body. The ONLY place request field names are spelled."""
    wire_questions: dict[str, Any] = {}
    for q in questions:
        if isinstance(q, Choice):
            wire_questions[q.id] = {
                "type": "choice",
                "instructions": q.prompt,
                # Option -> rubric. ``None`` means "no extra detail", which is
                # exactly our case: the option string IS the description.
                "criteria": {opt: None for opt in q.options},
            }
        elif isinstance(q, Noul):
            wire_questions[q.id] = {"type": "noul", "instructions": q.prompt}
        else:  # pragma: no cover - the union is closed
            raise JevProtocolError(f"unsupported question type {type(q).__name__}")
    return {"state": state, "model": model, "questions": wire_questions}


def _from_wire(body: Any, questions: list[Question]) -> Answers:
    """Parse a response body. The ONLY place response field names are spelled.

    Raises :class:`JevProtocolError` when a question went unanswered or an
    answer's own type field disagrees with what was asked. Partial results are
    refused on purpose: the seam's contract is all-or-nothing (see
    ``oracle.py``), so half a mapping would reach a point file that has no way to
    ask which half it got.
    """
    if not isinstance(body, dict):
        raise JevProtocolError(f"response is {type(body).__name__}, not an object")
    raw_answers = body.get("answers")
    if not isinstance(raw_answers, dict):
        raise JevProtocolError("response has no 'answers' object")

    answers: Answers = {}
    for q in questions:
        raw = raw_answers.get(q.id)
        if not isinstance(raw, dict):
            raise JevProtocolError(f"no answer for question {q.id!r}")
        answers[q.id] = _answer_from_wire(q, raw)

    # The wire's ``usage`` block is read past rather than parsed: the log records
    # latency and failure, not spend, so a token count here would only produce a
    # field no reader has.
    return answers


def _answer_from_wire(q: Question, raw: dict) -> Answer:
    """One answer, mapped back into the question's own units."""
    probabilities = raw.get("probabilities")
    probabilities = probabilities if isinstance(probabilities, dict) else {}
    confidence = _as_float_or_none(raw.get("confidence"))

    if isinstance(q, Choice):
        chosen = raw.get("choice")
        if not isinstance(chosen, str):
            raise JevProtocolError(f"answer for {q.id!r} has no 'choice' string")
        # Not validated against ``q.options``: the API answers from the criteria
        # map we sent, so a mismatch would mean our own request was wrong, and
        # the honest failure for that is the point file seeing the value it got.
        return Answer(
            id=q.id,
            value=chosen,
            p=_as_float(probabilities.get(chosen)),
            confidence=confidence,
        )

    if isinstance(q, Noul):
        noul = raw.get("noul")
        if not isinstance(noul, (int, float)) or isinstance(noul, bool):
            raise JevProtocolError(f"answer for {q.id!r} has no numeric 'noul'")
        value = float(noul)
        # value == p by construction here: the answer IS a probability. The API
        # reports no confidence for this type, so it stays None -- see the module
        # docstring and types.py on why None must not be read as 0.0.
        return Answer(id=q.id, value=value, p=value, confidence=confidence)

    raise JevProtocolError(f"unsupported question type {type(q).__name__}")


def _as_float(raw: Any) -> float:
    """*raw* as a float, or 0.0. Never raises."""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _as_float_or_none(raw: Any) -> float | None:
    """*raw* as a float, or ``None`` when absent/unreadable.

    Distinct from :func:`_as_float` because ``confidence`` has a meaningful
    "not reported" state that 0.0 would misrepresent as certainty of nothing.
    """
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class JevOracle:
    """Implements :class:`~kiro_crew.decisions.oracle.DecisionOracle` over HTTP."""

    def __init__(self, provider: Any) -> None:
        self._endpoint = str(getattr(provider, "endpoint", "") or _DEFAULT_ENDPOINT)
        self._model = str(getattr(provider, "model", "") or _DEFAULT_MODEL)
        self._api_key_setting = str(getattr(provider, "api_key", "") or "")
        # Transport-level deadline only. The gate owns the real budget; this
        # makes the socket give up at the same moment rather than leaving a
        # connection open past a cancelled ``wait_for``.
        self._timeout_ms = getattr(provider, "timeout_ms", 1000)

    async def ask(self, state: dict | str, questions: list[Question]) -> Answers:
        """POST one request carrying every question. Raises on any failure."""
        if not questions:
            raise JevProtocolError("no questions to ask")
        # Off the loop: the vault reads and decrypts a file, which is
        # filesystem work, and `no-blocking-call-on-event-loop` (blocking: true)
        # names that. Same remedy as the log append and the skill-tree walk.
        api_key = await asyncio.to_thread(
            resolve_api_key, self._api_key_setting, endpoint=self._endpoint
        )
        if not api_key:
            # Refused here rather than sent as an empty bearer: see
            # ``resolve_api_key``. A missing key is a config finding, and it must
            # read differently in the log from the provider rejecting a real one.
            raise JevProtocolError("no api key configured")

        # aiohttp is a hard dependency of the package but a heavy import; keep it
        # off the import path of a config where every point is off.
        import aiohttp

        body = _to_wire(state, self._model, questions)
        timeout = aiohttp.ClientTimeout(total=max(0.001, _as_float(self._timeout_ms) / 1000.0))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                self._endpoint,
                json=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            ) as resp:
                # Read the body before branching on status: an error body carries
                # the offending field on a 422, and that text is the whole value
                # of the row's ``error``. Bounded, because ``resp.text()`` reads
                # to EOF and would allocate an unbounded body before any check.
                # Chunked, not one `read(n)`: a StreamReader may return fewer
                # bytes than asked for while more are still coming, so a single
                # read plus a length check silently yields a TRUNCATED body on
                # exactly the oversized input the check exists to refuse. This
                # holds at most one chunk beyond the cap and refuses on the real
                # total.
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(_RESPONSE_CHUNK_BYTES):
                    total += len(chunk)
                    if total > _MAX_RESPONSE_BYTES:
                        raise JevProtocolError(f"response exceeded {_MAX_RESPONSE_BYTES} bytes")
                    chunks.append(chunk)
                text = b"".join(chunks).decode("utf-8", errors="replace")
                if resp.status < 200 or resp.status >= 300:
                    raise JevHttpError(resp.status, text[:200])
                import json

                try:
                    parsed = json.loads(text)
                except ValueError as exc:
                    raise JevProtocolError(f"response is not JSON: {exc}") from exc
        return _from_wire(parsed, questions)
