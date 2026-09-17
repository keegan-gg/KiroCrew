"""``decide`` -- the one function a point calls, and every refusal in front of it.

Four refusals, cheapest first, each returning ``None``: ``decisions.enabled`` is
not exactly ``True``; *point* is not in :data:`DECISION_POINT_NAMES`; the session
hashes outside ``decisions.bucket``; the state or a question carries a credential
or an exfiltration-shaped URL. Then the call itself, which returns ``None`` on a
timeout, a provider failure, or an answer outside the declared domain.

The first three write nothing at all, which is what makes "``enabled=false``
leaves the log directory empty" checkable. The scrub and the call failures write
one row, because a silent scrub is the worst outcome available: the operator would
read a missing row as "not firing" when the seam fires and refuses every time.

Two properties the order is chosen for. ``enabled`` is read before any ``await``,
so a disabled seam is provably inert rather than merely fast (pinned by
``test_decisions_gate.TestDisabledPerformsNoAwait``). And the scrub sits BEFORE
the network, so no transport can exist above it.

Config comes from the live watcher's snapshot, never from disk. Anything
unreadable -- no snapshot, no section, a non-bool ``enabled``, a bucket that is
not a number, an attribute read that raises -- resolves to OFF: fail-closed is the
only safe direction for a gate whose open state sends conversation text to a third
party.

Two budgets, both named here so a caller can size its own outer wait: the provider
call is bounded by :func:`timeout_secs`, and the row write by
:data:`_LOG_BUDGET_SECS` on top of it. Nothing this module logs carries a provider
message or a traceback -- a row's ``error`` is one of the identifiers below and the
application log gets the exception CLASS only, because both artifacts are readable
and a provider can quote the request back.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from hashlib import sha256
from typing import Any

from kiro_crew import credential_patterns as _cred
from kiro_crew.decisions import log as _log
from kiro_crew.decisions.types import Answer, Answers, Choice, Noul, Question

logger = logging.getLogger(__name__)

#: Decision points this build ships; an absent name is refused. Lives with the
#: seam, not in ``config.sections``: nothing in the config is keyed by point name,
#: and keeping it here keeps the config loader off a hot path's import graph.
DECISION_POINT_NAMES = ("skills.select",)

#: Bucket modulus and upper clamp, matching the config's declared 0..100 bounds.
_BUCKET_MOD = 100

#: Provider budget used when the config carries no usable ``timeout_ms``.
_DEFAULT_TIMEOUT_MS = 1000.0
_MIN_TIMEOUT_SECS = 0.001

#: How long the row write may hold the caller, on top of the provider budget. The
#: write is one ``O_APPEND`` of a few hundred bytes, so this exists only so a
#: stalled filesystem cannot make an observation cost the turn. On expiry the
#: awaiting side gives up and ``decide`` returns; the worker thread is NOT
#: cancellable, so the append may still land afterwards -- acceptable for a write
#: that cannot corrupt a line. An outer wait therefore needs ``timeout_secs()``
#: plus this.
_LOG_BUDGET_SECS = 0.05

#: A row's ``error`` is one of these -- an identifier an operator can act on, never
#: a provider message, which is unbounded and can quote the request back.
ERROR_TIMEOUT = "timeout"
ERROR_PROVIDER = "provider"
ERROR_INVALID_RESULT = "invalid-result"
ERROR_SCRUBBED_CREDENTIAL = "scrubbed:credential"
ERROR_SCRUBBED_URL = "scrubbed:exfiltration-url"
ERROR_SCRUBBED_SCAN_FAILED = "scrubbed:scan-failed"

#: The categories that mean nothing was sent; a row's ``scrubbed`` flag is
#: membership here rather than a prefix match.
SCRUB_ERRORS = (ERROR_SCRUBBED_CREDENTIAL, ERROR_SCRUBBED_URL, ERROR_SCRUBBED_SCAN_FAILED)

#: Credential spellings refused locally, before the canonical scanner. Compiled
#: here because ``credential_patterns`` exports pattern SOURCE strings. This is the
#: scrubber-side AWS spelling, not the wider redaction one, and not the whole of
#: the scrub: ``VENDOR_TOKEN_PATTERNS`` carries a generic ``sk-`` form that
#: ``redact_credentials`` does not, which is why both run.
_CREDENTIAL_RE = re.compile(
    "|".join(
        [_cred.AWS_KEY_ID, _cred.JWT_MULTI_SEGMENT]
        + [frag for _label, frag in _cred.VENDOR_TOKEN_PATTERNS]
    )
)


def _probability(value: object) -> bool:
    """Whether *value* is a real number in 0..1 (a bool is not a probability)."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0.0 <= float(value) <= 1.0
    )


def _answers_are_valid(answers: Answers, questions: list[Question]) -> bool:
    """Whether every question got exactly one answer inside its own domain."""
    by_id = {question.id: question for question in questions}
    if set(answers) != set(by_id):
        return False
    for question_id, question in by_id.items():
        answer = answers[question_id]
        if not isinstance(answer, Answer) or answer.id != question_id:
            return False
        if not _probability(answer.p):
            return False
        if isinstance(question, Choice) and answer.value not in question.options:
            return False
        if isinstance(question, Noul) and not _probability(answer.value):
            return False
    return True


def _snapshot() -> Any:
    """The live config snapshot, or ``None``.

    The ``config.live`` import is function-local because it pulls the whole loader
    in, and this package is imported lazily from a hot path precisely to avoid that.
    """
    from kiro_crew.config import live

    return live.snapshot()


def _decisions_config(config: Any | None) -> Any | None:
    """The ``decisions`` section of *config*, or of the live snapshot.

    *config* is an injection seam for tests and for a caller that already holds a
    config; ``None`` means "read the snapshot".
    """
    cfg = config if config is not None else _snapshot()
    return None if cfg is None else getattr(cfg, "decisions", None)


def in_bucket(session_key: str | None, bucket: object) -> bool:
    """Whether *session_key* falls inside a *bucket*-percent sample.

    The digest is the SAME one the log's ``session`` field carries, so a row is
    enough to re-derive why it was sampled without keeping the key.

    ``0`` admits nothing and ``100`` admits everything, both as closed forms. An
    out-of-range or unreadable value is clamped rather than rejected: ``enabled``
    is the switch, and a typo'd bucket must not become a second, undocumented way
    to disable the seam.
    """
    try:
        wanted = int(bucket)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        wanted = _BUCKET_MOD
    wanted = max(0, min(_BUCKET_MOD, wanted))
    digest = sha256((session_key or "").encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % _BUCKET_MOD < wanted


def timeout_secs(config: Any | None = None) -> float:
    """The budget one ``decide`` call is bounded by, in seconds. Never raises.

    Public because a caller that schedules ``decide`` as a task needs the SAME
    number for its outer wait: a helper waiting for a budget it invented would
    either abandon a call the gate was still going to answer, or wait past the
    deadline the gate already enforces.

    Always finite and positive, so a missing, non-numeric, infinite or
    non-positive ``timeout_ms`` cannot become a value ``wait_for`` rejects or a
    deadline that never expires.
    """
    try:
        provider = getattr(_decisions_config(config), "provider", None)
        ms = float(getattr(provider, "timeout_ms", _DEFAULT_TIMEOUT_MS))
    except Exception:
        ms = _DEFAULT_TIMEOUT_MS
    if not math.isfinite(ms):
        ms = _DEFAULT_TIMEOUT_MS
    return max(_MIN_TIMEOUT_SECS, ms / 1000.0)


def _scan_text(state: dict | str, questions: list[Question]) -> str:
    """Everything the scrub must clear, as one string.

    The questions are scanned alongside the state because they leave the machine in
    the same request: "the state was clean" says nothing about the rubric sent with
    it. A ``dict`` is rendered with ``json.dumps`` rather than ``str()``, which can
    elide content behind a ``__repr__`` -- rendering the way the wire will is what
    makes the scan see what the wire sees.
    """
    if isinstance(state, str):
        rendered = state
    else:
        import json

        try:
            rendered = json.dumps(state, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            rendered = repr(state)
    parts = [rendered]
    for question in questions:
        parts.append(str(getattr(question, "prompt", "") or ""))
        parts.extend(str(option) for option in getattr(question, "options", ()) or ())
    return "\n".join(parts)


def scrub_reason(state: dict | str, questions: list[Question]) -> str | None:
    """A :data:`SCRUB_ERRORS` category, or ``None`` when the request may be sent.

    Local regex first, then both canonical scanners; any warning refuses the whole
    request. A scanner that itself fails also refuses -- an external request cannot
    be cleared by a scan that did not complete. The reason is a category, never the
    matched text, which would put the credential into the log.
    """
    text = _scan_text(state, questions)
    if _CREDENTIAL_RE.search(text) is not None:
        return ERROR_SCRUBBED_CREDENTIAL
    try:
        from kiro_crew.security.redaction import redact_credentials

        _cleaned, warnings = redact_credentials(text)
    except Exception:
        logger.warning("decisions: credential scan failed; refusing the state")
        return ERROR_SCRUBBED_SCAN_FAILED
    if warnings:
        return ERROR_SCRUBBED_CREDENTIAL
    try:
        from kiro_crew.security import redact_exfiltration_urls

        _cleaned, warnings = redact_exfiltration_urls(text)
    except Exception:
        logger.warning("decisions: exfiltration URL scan failed; refusing the state")
        return ERROR_SCRUBBED_SCAN_FAILED
    return ERROR_SCRUBBED_URL if warnings else None


def _sampled(point: str, session_key: str | None, config: Any | None) -> bool:
    """Refusals 1-3, shared by :func:`is_enabled` and :func:`decide` so the two
    cannot drift. No ``await``, no IO, no import of an implementation."""
    decisions = _decisions_config(config)
    if decisions is None or getattr(decisions, "enabled", False) is not True:
        return False
    if point not in DECISION_POINT_NAMES:
        # WARNING, not debug: the caller is code in this repo, so an unknown name
        # is a typo in a point file rather than an operator's config.
        logger.warning("decisions: unknown point %r (known: %s)", point, DECISION_POINT_NAMES)
        return False
    return in_bucket(session_key, getattr(decisions, "bucket", _BUCKET_MOD))


def is_enabled(point: str, *, session_key: str | None = None, config: Any | None = None) -> bool:
    """Whether *point* would get past refusals 1-3 right now. Never raises.

    For a hook whose STATE is expensive to build -- walking the skill tree, reading
    frontmatter -- which would otherwise do that work on the default configuration
    and hand it to a ``decide`` that refuses on its first line. It is not a second
    gate and grants nothing: ``decide`` re-runs every refusal, so skipping it is
    merely wasteful and racing a config change costs one row.
    """
    try:
        return _sampled(point, session_key, config)
    except Exception as exc:
        logger.debug("decisions: is_enabled(%s) failed (%s)", point, type(exc).__name__)
        return False


async def decide(
    point: str,
    state: dict | str,
    questions: list[Question],
    *,
    session_key: str | None = None,
    config: Any | None = None,
) -> Answers | None:
    """Ask *questions* about *state* at *point*, or return ``None``.

    ``None`` is the ONLY failure signal and it is never exceptional: every refusal,
    every provider error, every timeout and an unreadable config all return it. A
    caller therefore needs no try/except and no enable check of its own --
    ``answers = await decide(...)`` then ``if answers is None: <existing
    behaviour>`` is the complete integration.

    ``asyncio.CancelledError`` is the one exception that propagates: cancellation
    is the caller going away, not a decision failure, and swallowing it would break
    structured concurrency.

    *config* injects a config instead of reading the live snapshot; production
    callers leave it unset.
    """
    # Guarded because *config* may be an arbitrary object whose attribute reads
    # raise, and this seam must never alter the turn it sits in.
    try:
        if not _sampled(point, session_key, config):
            return None
        budget = timeout_secs(config)
        provider = getattr(_decisions_config(config), "provider", None)
    except Exception as exc:
        logger.debug("decisions: %s config read failed (%s)", point, type(exc).__name__)
        return None

    async def _write(*, latency_ms: int, answers: Answers | None, error: str | None) -> None:
        # Guarded here as well as inside ``log.append``: ``append`` protects the
        # WRITE, this protects BUILDING the row, which renders values an
        # implementation supplied. The class only, never a message, for the same
        # reason.
        try:
            row = _log.build_row(
                point=point,
                session_key=session_key,
                latency_ms=latency_ms,
                answers=answers,
                scrubbed=error in SCRUB_ERRORS,
                error=error,
            )
            await asyncio.wait_for(asyncio.to_thread(_log.append, row), _LOG_BUDGET_SECS)
        except Exception as exc:
            logger.warning("decisions: could not record %s row (%s)", point, type(exc).__name__)

    refusal = scrub_reason(state, questions)
    if refusal is not None:
        await _write(latency_ms=0, answers=None, error=refusal)
        return None

    started = time.monotonic()
    try:
        from kiro_crew.decisions.impl_jev import JevOracle

        answers = await asyncio.wait_for(JevOracle(provider).ask(state, questions), timeout=budget)
    except asyncio.TimeoutError:
        # Named apart from the generic branch: "timeout" is the one failure an
        # operator can act on mechanically (raise timeout_ms, or accept the rate).
        await _write(latency_ms=_elapsed_ms(started), answers=None, error=ERROR_TIMEOUT)
        return None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # The class, and no traceback: a provider message -- and the locals a
        # traceback renders -- can quote the request back. The class still
        # separates a transport failure from a protocol one.
        logger.debug("decisions: %s provider call failed (%s)", point, type(exc).__name__)
        await _write(latency_ms=_elapsed_ms(started), answers=None, error=ERROR_PROVIDER)
        return None

    latency_ms = _elapsed_ms(started)
    if not answers or not _answers_are_valid(answers, questions):
        # An implementation returning an empty or out-of-domain mapping broke its
        # contract (oracle.py: raise, never return either), so it is recorded as an
        # error rather than as an answer.
        await _write(latency_ms=latency_ms, answers=None, error=ERROR_INVALID_RESULT)
        return None

    # Written with the answers in hand, outside ``budget`` and under
    # ``_LOG_BUDGET_SECS``: the write cannot spend the provider deadline, cannot
    # hold the caller longer than that budget, and cannot cost it the result.
    await _write(latency_ms=latency_ms, answers=answers, error=None)
    return answers


def _elapsed_ms(started: float) -> int:
    """Whole milliseconds since *started* (a ``time.monotonic()`` reading)."""
    return int((time.monotonic() - started) * 1000)
