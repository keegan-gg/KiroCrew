"""The decision log: the row's shape, its bounds, and the append's permissions.

Two groups carry the weight. :class:`TestRowShape` pins what the row does NOT
contain — the state, the session key, a provider message — because the row is
written to disk on a path whose whole point is that conversation text does not
land there. :class:`TestAppend`'s permission assertions are the other half: the
row names no credential, but it does reveal when the seam fires and with what
verdicts, so the directory is 0700 and a fresh file 0600 REGARDLESS of umask.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone

import pytest

from kiro_crew.decisions import log as log_mod
from kiro_crew.decisions.log import append, build_row, log_path, session_digest
from kiro_crew.decisions.types import Answer


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect the log directory into *tmp_path* and hand back its path."""
    directory = tmp_path / "decisions"
    monkeypatch.setattr(log_mod, "log_dir", lambda: directory)
    return directory


def _row(**kw):
    base = dict(point="skills.select", session_key="sess-1", latency_ms=212)
    base.update(kw)
    return build_row(**base)


class TestRowShape:
    def test_the_row_is_exactly_these_seven_fields(self):
        """A field added here is a field every later reader must tolerate."""
        assert set(_row()) == {
            "ts",
            "point",
            "session",
            "latency_ms",
            "scrubbed",
            "answers",
            "error",
        }

    def test_answers_are_flattened_to_value_p_confidence(self):
        row = _row(answers={"verdict": Answer("verdict", "DUP", 0.9, 0.8)})
        assert row["answers"] == {"verdict": {"value": "DUP", "p": 0.9, "confidence": 0.8}}

    def test_no_answers_is_null_not_an_empty_object(self):
        assert _row()["answers"] is None
        assert _row(answers={})["answers"] is None

    def test_the_session_key_is_never_written_verbatim(self):
        row = _row(session_key="chat-with-my-manager")
        assert "chat-with-my-manager" not in json.dumps(row)
        assert row["session"] == session_digest("chat-with-my-manager")

    def test_the_digest_is_stable_and_keyless_calls_share_a_bucket(self):
        assert _row(session_key="k")["session"] == _row(session_key="k")["session"]
        assert _row(session_key=None)["session"] == _row(session_key="")["session"]

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (True, True),
            (False, False),
            (0, False),
            (1, False),
            ("scrubbed:credential", False),
            (["credential"], False),
            (None, False),
        ],
    )
    def test_scrubbed_is_exactly_true_or_false(self, raw, expected):
        """Only ``True`` reads as refused: a truthy stand-in must not.

        A category string or a count in this field would read as "nothing left the
        machine" while carrying something else, and this is the one field a reader
        must be able to trust as a boolean.
        """
        assert _row(scrubbed=raw)["scrubbed"] is expected

    def test_ts_is_utc_and_parseable(self):
        moment = datetime.fromisoformat(_row()["ts"])
        assert moment.tzinfo is not None
        assert moment.utcoffset().total_seconds() == 0


class TestBounds:
    """One malformed provider reply must not write an unbounded line."""

    def test_a_long_value_is_clipped(self):
        row = _row(answers={"v": Answer("v", "x" * 5000, 1.0)})
        assert len(row["answers"]["v"]["value"]) == log_mod._MAX_VALUE_CHARS

    def test_a_number_survives_as_a_number(self):
        """Clipping must not turn a probability into a string a reader has to parse."""
        row = _row(answers={"v": Answer("v", 0.25, 0.25)})
        assert row["answers"]["v"]["value"] == 0.25

    def test_too_many_answers_are_capped(self):
        answers = {f"q{i}": Answer(f"q{i}", "A", 0.5) for i in range(50)}
        assert len(_row(answers=answers)["answers"]) == log_mod._MAX_ANSWERS

    def test_an_unbounded_repr_is_still_bounded(self):
        class _Big:
            def __repr__(self):
                return "y" * 9000

        row = _row(answers={"v": Answer("v", _Big(), 1.0)})
        assert len(row["answers"]["v"]["value"]) == log_mod._MAX_VALUE_CHARS


class TestAppend:
    def test_a_row_lands_as_one_json_line(self, home):
        append(_row())
        text = log_path().read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert len(text.strip().splitlines()) == 1
        assert json.loads(text)["point"] == "skills.select"

    def test_rows_accumulate_rather_than_replace(self, home):
        for i in range(5):
            append(_row(latency_ms=i))
        lines = log_path().read_text(encoding="utf-8").strip().splitlines()
        assert [json.loads(ln)["latency_ms"] for ln in lines] == [0, 1, 2, 3, 4]

    @pytest.mark.skipif(
        os.name != "posix",
        reason="0700 is a POSIX mode; Windows reports 0o777 whatever the open requested",
    )
    def test_the_directory_is_owner_only(self, home):
        append(_row())
        mode = stat.S_IMODE(os.stat(home).st_mode)
        assert mode == 0o700, oct(mode)

    @pytest.mark.skipif(
        os.name != "posix",
        reason="0600 and umask are POSIX; Windows reports 0o666 and has no umask",
    )
    def test_a_fresh_file_is_owner_only_despite_a_permissive_umask(self, home):
        """0600 must come from the open mode, not from inherited umask luck."""
        previous = os.umask(0o000)
        try:
            append(_row())
        finally:
            os.umask(previous)
        mode = stat.S_IMODE(os.stat(log_path()).st_mode)
        assert mode == 0o600, oct(mode)

    def test_the_filename_carries_the_utc_day(self, home):
        append(_row())
        today = datetime.now(timezone.utc).date()
        assert log_path().name == f"decisions-{today:%Y%m%d}.jsonl"

    @pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform has no O_NOFOLLOW")
    def test_append_refuses_a_symlinked_day_log(self, home, tmp_path):
        home.mkdir(parents=True)
        target = tmp_path / "protected.txt"
        target.write_text("unchanged", encoding="utf-8")
        log_path().symlink_to(target)

        append(_row())

        assert target.read_text(encoding="utf-8") == "unchanged"
        assert log_path().is_symlink()

    def test_an_unwritable_home_is_survived_not_raised(self, tmp_path, monkeypatch):
        """Best-effort by contract: an observation must not fail a turn."""
        blocked = tmp_path / "blocked"
        blocked.write_text("i am a file, not a directory", encoding="utf-8")
        monkeypatch.setattr(log_mod, "log_dir", lambda: blocked / "decisions")
        append(_row())  # must not raise

    def test_an_unserialisable_value_is_survived(self, home):
        """``default=str`` keeps an odd value from losing the whole row."""

        class _Odd:
            def __str__(self):
                return "odd-value"

            def __repr__(self):
                return "odd-value"

        append(_row(answers={"v": Answer("v", _Odd(), 1.0)}))
        row = json.loads(log_path().read_text(encoding="utf-8"))
        assert row["answers"]["v"]["value"] == "odd-value"
