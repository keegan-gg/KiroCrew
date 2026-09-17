"""The decision log: one JSONL line per decision the gate actually attempted.

``~/.kiro/crew/decisions/decisions-YYYYMMDD.jsonl``, mode 0600 in a 0700
directory. Day-rotated by filename so a retention sweep is ``unlink`` on whole
files rather than a rewrite, and so a reader can bound its work by date without
parsing.

What is NOT logged
------------------
A row exists only where a decision was ATTEMPTED. The three cheap refusals --
``enabled`` off, an unknown point, outside the bucket -- write nothing, which is
what makes "``enabled=false`` leaves the log directory empty" a checkable claim
rather than a hope. Logging them would also invert the cost: the whole point of
the ``enabled`` gate is that a disabled seam touches no disk.

A scrub hit and a provider error DO get a row, because both are findings. A
silent scrub is the worst outcome available here: the operator would see a
missing row and conclude the seam is not firing, when in fact it fires and
refuses every time.

The row is six fields, and the omissions are the design
------------------------------------------------------
``ts``, ``point``, ``session``, ``latency_ms``, ``scrubbed``, ``error``, plus a
bounded ``answers``. That is enough to answer "is it firing, how slow is it, how
often does it fail, and what did it say".

``state`` is absent: it leaves the machine when the seam is enabled, but it does
not also get written to disk, so this file never becomes a second copy of the
conversation. ``session`` is a truncated SHA-256 of the session key for the same
reason -- it groups rows without naming a chat. ``error`` is a CATEGORY chosen by
the gate, never a provider message: a message is unbounded and can quote the
request back, which is how a credential would reach the file the scrub exists to
keep it out of.

Why one synchronous append helper
---------------------------------
The gate offloads this helper as one unit, including open, write and close, so
none of its filesystem calls run on the event loop. The helper still performs
one ``O_APPEND`` write of a few hundred bytes, with no lock and no
read-modify-write, so concurrent calls cannot interleave a line.
``atomic_write`` is the wrong tool here in the other direction: it replaces the
whole file, so appending line N would rewrite N-1 lines and turn a constant-cost
write into a linear one.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from kiro_crew.config.paths import config_dir
from kiro_crew.decisions.types import Answers

logger = logging.getLogger(__name__)

#: Directory mode. 0700 so the log is unreadable by other local accounts even
#: before the per-file mode applies -- a decision row names no secret, but it
#: does reveal when the seam fires, how often, and with what verdicts.
_DIR_MODE = 0o700

#: File mode for a fresh log file. Applied via ``os.open``'s mode argument,
#: which the umask can only NARROW, so a permissive umask cannot widen it.
_FILE_MODE = 0o600

#: Refuse a symlink at the log-file leaf atomically with the open. Platforms
#: without ``O_NOFOLLOW`` do not expose the flag; their open semantics apply.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

#: Characters of hex kept from the session-key digest. 12 hex = 48 bits: enough
#: that two live sessions colliding is not a practical concern, short enough
#: that the value is obviously an opaque grouping key and not a handle.
_SESSION_HEX = 12

#: Filename stem. The date suffix and ``.jsonl`` are appended.
_STEM = "decisions-"

#: Most answers written to one row. A point asks a handful of questions; a
#: mapping larger than this is a broken implementation, and the row exists to
#: record that it happened, not to hold the whole of it.
_MAX_ANSWERS = 8

#: Longest rendered answer ``value``. A ``Choice`` value is one of the options the
#: point declared, so this is not a truncation anyone should ever see -- it is the
#: bound that keeps one malformed provider reply from writing an unbounded line
#: into a file every later row shares.
_MAX_VALUE_CHARS = 200


def log_dir() -> Path:
    """``~/.kiro/crew/decisions`` -- not created by this call."""
    return config_dir() / "decisions"


def log_path(when: date | None = None) -> Path:
    """The log file for *when* (default: today, UTC).

    UTC, not local time, so a row's filename and its own ``ts`` never disagree
    about which day it belongs to -- a reader that bounds work by filename would
    otherwise miss rows either side of a timezone offset.
    """
    day = when or datetime.now(timezone.utc).date()
    return log_dir() / f"{_STEM}{day:%Y%m%d}.jsonl"


def session_digest(session_key: str | None) -> str:
    """Truncated SHA-256 of *session_key*, for grouping rows without naming one.

    ``None`` and ``""`` both hash the empty string, so a row from a keyless
    call is grouped with the other keyless rows rather than carrying a
    distinguishable ``null``. This is the SAME digest the bucket decision reads,
    so a row's ``session`` value is enough to re-derive why it was sampled.
    """
    return sha256((session_key or "").encode("utf-8")).hexdigest()[:_SESSION_HEX]


def _bounded_value(value: object) -> object:
    """*value* as something bounded and JSON-safe.

    A number or a bool goes through unchanged -- both are already bounded and a
    reader that has to parse ``"0.9"`` back out of a string is worse off. Anything
    else is rendered as text and clipped, which is what keeps one row from
    growing without limit.
    """
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    text = value if isinstance(value, str) else repr(value)
    return text[:_MAX_VALUE_CHARS]


def _answers_json(answers: Answers | None) -> dict[str, dict[str, Any]] | None:
    """``{id: {value, p, confidence}}`` -- bounded -- or ``None`` when empty."""
    if not answers:
        return None
    out: dict[str, dict[str, Any]] = {}
    for qid, answer in list(answers.items())[:_MAX_ANSWERS]:
        out[str(qid)[:_MAX_VALUE_CHARS]] = {
            "value": _bounded_value(answer.value),
            "p": answer.p,
            "confidence": answer.confidence,
        }
    return out


def build_row(
    *,
    point: str,
    session_key: str | None,
    latency_ms: int,
    answers: Answers | None = None,
    scrubbed: bool = False,
    error: str | None = None,
    ts: datetime | None = None,
) -> dict[str, Any]:
    """The row :func:`append` writes, built without touching the filesystem.

    Split out from the write so a test can assert the SHAPE without a temp home,
    and so the gate can build a row it then decides not to write.

    ``scrubbed`` is written as ``scrubbed is True`` and not ``bool(scrubbed)``:
    "did anything leave the machine" is the one field a reader must be able to
    trust as an exact boolean, and a truthy stand-in -- a category string, a
    count -- would silently read as "refused" while carrying something else.
    """
    moment = ts or datetime.now(timezone.utc)
    return {
        "ts": moment.isoformat(),
        "point": point,
        "session": session_digest(session_key),
        "latency_ms": latency_ms,
        "scrubbed": scrubbed is True,
        "answers": _answers_json(answers),
        "error": error,
    }


def append(row: dict[str, Any]) -> None:
    """Append *row* as one JSON line. Never raises.

    Best-effort by contract: the seam is an observation, so a read-only home, a
    full disk or a directory someone chmod-ed must not turn into a failed turn
    at the call site. The failure is logged at WARNING (not DEBUG) precisely
    because a silently missing log looks identical to a seam that is switched
    off, and an operator reading an empty directory deserves to find out which.
    """
    try:
        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
        # O_APPEND: the kernel places each write at the then-current end of file,
        # so two calls writing at once cannot interleave. The mode argument
        # applies only when this call CREATES the file; an existing file keeps
        # whatever mode it has, which is why day-rotation is the moment the
        # 0600 is actually established.
        fd = os.open(
            str(log_path()),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | _O_NOFOLLOW,
            _FILE_MODE,
        )
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception as exc:
        logger.warning("decisions: could not append log row: %s", exc)
