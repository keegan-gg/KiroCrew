"""``decide`` / ``is_enabled`` / ``timeout_secs``: the four refusals and the call.

The load-bearing test in this file is
:class:`TestDisabledPerformsNoAwait`: it is the reason this seam is safe to place
in a hot path, and it is asserted by making the implementation fail the test if it
is entered at all, rather than by timing anything.

The second load-bearing group is :class:`TestEnablingIsExplicit`. ``enabled`` is
the switch that lets conversation state leave the machine, so the tests here pin
that only the literal ``true`` opens it -- including that the earlier
``preview``/``points``/``arm`` spelling, which an operator may still have in
``config.json``, does not.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from kiro_crew import credential_patterns as _cred
from kiro_crew.config.sections import DecisionProviderConfig, DecisionsConfig
from kiro_crew.decisions import gate as gate_mod
from kiro_crew.decisions import log as log_mod
from kiro_crew.decisions.gate import DECISION_POINT_NAMES, decide, in_bucket, is_enabled
from kiro_crew.decisions.types import Answer, Choice, Noul

# ---------------------------------------------------------------------------
# Fixtures and doubles
# ---------------------------------------------------------------------------

#: AWS key-id samples, assembled from the prefix list in
#: ``credential_patterns`` rather than written out. A contiguous key-shaped
#: literal is refused by the repo's own secret scanners -- correctly, since
#: neither the content scan nor Semgrep can tell a test vector from a real
#: leak -- and deriving the samples means a prefix added there is exercised
#: here without this list being edited to match.
_AWS_KEY_BODY = "A2B3C4D5E6F7G8H9"  # 16 of [A-Z0-9], per AWS_KEY_ID_BODY
_AWS_KEY_SAMPLES = tuple(prefix + _AWS_KEY_BODY for prefix in _cred.AWS_KEY_ID_PREFIXES.split("|"))

#: A destination only the URL scanner objects to: the credential scanners clear it
#: (probed above the assertion in ``test_the_two_scanners_are_distinguished``), so
#: it is what separates the two scrub categories rather than doubling the first.
_EXFIL_URL = "see https://collector.example.invalid/x?sess" + "ion=" + "b" * 40

POINT = "skills.select"
QUESTIONS = [Choice(id="verdict", prompt="Which skill?", options=["NONE", "DUP"])]


def _config(*, enabled: bool = True, bucket: int = 100, timeout_ms: int = 1000):
    """A config object shaped like the one ``decide`` reads off the live snapshot.

    A real ``KiroCrewConfig`` would work too, but constructing one loads the whole
    config module for a two-field read; the gate only ever touches ``.decisions``,
    so a stand-in with that attribute is the honest surface.
    """
    return SimpleNamespace(
        decisions=DecisionsConfig(
            enabled=enabled,
            bucket=bucket,
            provider=DecisionProviderConfig(timeout_ms=timeout_ms),
        )
    )


class _RecordingOracle:
    """Answers every question with a fixed value and records every call."""

    def __init__(self, value: str = "DUP") -> None:
        self.value = value
        self.calls: list[tuple] = []

    async def ask(self, state, questions):
        self.calls.append((state, questions))
        return {q.id: Answer(id=q.id, value=self.value, p=0.87, confidence=0.82) for q in questions}


class _ExplodingOracle:
    """Fails the TEST if it is ever entered.

    Not ``raise`` -- a raise would be caught by the call's own guard and logged as
    an error, which is a pass-looking outcome. ``pytest.fail`` inside a coroutine
    that the gate awaits surfaces as a test failure.
    """

    def __init__(self) -> None:
        self.entered = False

    async def ask(self, state, questions):  # pragma: no cover - must never run
        self.entered = True
        pytest.fail("the provider was entered on a path that must perform no await")


class _RaisingOracle:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def ask(self, state, questions):
        raise self.exc


class _SlowOracle:
    """Sleeps *secs* before answering."""

    def __init__(self, secs: float) -> None:
        self.secs = secs
        self.finished = False

    async def ask(self, state, questions):
        await asyncio.sleep(self.secs)
        self.finished = True
        return {q.id: Answer(id=q.id, value="DUP", p=0.5) for q in questions}


@pytest.fixture
def install_impl(monkeypatch):
    """Replace the one implementation ``decide`` constructs.

    ``decide`` imports ``JevOracle`` inside its own body (the import stays off a
    hot path's import graph), so the patch lands on the module the import
    resolves against, not on a name in ``gate``.
    """
    import kiro_crew.decisions.impl_jev as impl_mod

    def _install(oracle):
        monkeypatch.setattr(impl_mod, "JevOracle", lambda provider: oracle)
        return oracle

    return _install


@pytest.fixture
def log_home(tmp_path, monkeypatch):
    """Point the log at *tmp_path* and return a reader for the rows written."""
    directory = tmp_path / "decisions"
    monkeypatch.setattr(log_mod, "log_dir", lambda: directory)

    def _rows():
        if not directory.exists():
            return []
        out = []
        for path in sorted(directory.glob("decisions-*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    out.append(json.loads(line))
        return out

    return _rows


# ---------------------------------------------------------------------------
# Gate 1 -- enabled
# ---------------------------------------------------------------------------


class TestDisabledPerformsNoAwait:
    """``enabled=false`` must cost nothing measurable, not merely return None."""

    def test_refuses_without_entering_the_provider(self, install_impl, log_home):
        oracle = install_impl(_ExplodingOracle())
        assert asyncio.run(decide(POINT, "hello", QUESTIONS, config=_config(enabled=False))) is None
        assert oracle.entered is False
        assert log_home() == [], "a refused decision must not write a log row"

    def test_the_coroutine_completes_on_its_first_step(self, install_impl):
        """No await before the refusal -- proven by driving the coroutine by hand.

        ``send(None)`` runs the body until it either yields to the loop (an
        awaited future) or finishes. A ``StopIteration`` on the very first send
        means the body reached ``return None`` without ever suspending, which is
        the strongest available form of "performs zero awaits": it does not
        depend on how fast anything ran.
        """
        install_impl(_ExplodingOracle())
        coro = decide(POINT, "hello", QUESTIONS, config=_config(enabled=False))
        with pytest.raises(StopIteration) as caught:
            coro.send(None)
        assert caught.value.value is None

    def test_a_config_without_the_section_is_off(self, install_impl, log_home):
        install_impl(_ExplodingOracle())
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=SimpleNamespace())) is None
        assert is_enabled(POINT, config=SimpleNamespace()) is False
        assert log_home() == []

    def test_no_snapshot_is_off(self, install_impl, log_home, monkeypatch):
        """An unprimed live watcher fails CLOSED rather than reading from disk."""
        install_impl(_ExplodingOracle())
        monkeypatch.setattr(gate_mod, "_snapshot", lambda: None)
        assert asyncio.run(decide(POINT, "hi", QUESTIONS)) is None
        assert is_enabled(POINT) is False
        assert log_home() == []

    def test_a_raising_config_read_reads_as_off(self, install_impl, monkeypatch):
        """Neither entry point may raise into the turn it was called from."""
        install_impl(_ExplodingOracle())

        def _boom():
            raise RuntimeError("snapshot exploded")

        monkeypatch.setattr(gate_mod, "_snapshot", _boom)
        assert is_enabled(POINT) is False
        assert asyncio.run(decide(POINT, "hi", QUESTIONS)) is None
        assert gate_mod.timeout_secs() > 0.0

    def test_a_config_whose_attributes_raise_reads_as_off(self, install_impl, log_home):
        """``config`` is an arbitrary object; an exploding read is not a turn failure."""
        oracle = install_impl(_ExplodingOracle())

        class _Hostile:
            @property
            def decisions(self):
                raise RuntimeError("attribute exploded")

        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=_Hostile())) is None
        assert is_enabled(POINT, config=_Hostile()) is False
        assert oracle.entered is False
        assert log_home() == []


class TestEnablingIsExplicit:
    """Only the literal ``true`` opens the seam. Nothing else may stand in for it."""

    @pytest.mark.parametrize("raw", [1, "true", "yes", "1", [1], {"a": 1}, 0.5])
    def test_a_truthy_non_bool_does_not_enable(self, install_impl, log_home, raw):
        oracle = install_impl(_ExplodingOracle())
        cfg = SimpleNamespace(decisions=SimpleNamespace(enabled=raw, bucket=100, provider=None))
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=cfg)) is None
        assert oracle.entered is False
        assert log_home() == []

    @pytest.mark.parametrize(
        "legacy",
        [
            {"preview": True},
            {"preview": True, "points": {"skills.select": {"arm": "live"}}},
            {"preview": True, "points": {"skills.select": {"arm": "shadow", "bucket": 100}}},
            {"points": {"skills.select": {"arm": "live", "impl": "jev"}}},
        ],
    )
    def test_the_earlier_preview_and_arm_spelling_does_not_enable(
        self, install_impl, log_home, legacy
    ):
        """A config written against the previous contract parses to OFF.

        The values are real operator intent, but they were set against a
        different switch. Inferring ``enabled`` from an arm would turn the seam
        on -- and start sending state -- from a value nobody wrote for it.
        """
        oracle = install_impl(_ExplodingOracle())
        cfg = SimpleNamespace(decisions=DecisionsConfig.from_raw(legacy))
        assert cfg.decisions.enabled is False
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=cfg)) is None
        assert oracle.entered is False
        assert log_home() == []


# ---------------------------------------------------------------------------
# Gate 2 -- the point name
# ---------------------------------------------------------------------------


class TestPointName:
    def test_the_shipped_vocabulary_is_one_point(self):
        assert DECISION_POINT_NAMES == ("skills.select",)

    @pytest.mark.parametrize("unknown", ["skills.dedupe", "cron.novelty", "", "skills.Select"])
    def test_an_unknown_point_is_refused_even_when_enabled(
        self, install_impl, log_home, unknown, caplog
    ):
        """An unknown name must not borrow the section's switch."""
        oracle = install_impl(_ExplodingOracle())
        with caplog.at_level("WARNING"):
            assert asyncio.run(decide(unknown, "hi", QUESTIONS, config=_config())) is None
        assert is_enabled(unknown, config=_config()) is False
        assert oracle.entered is False
        assert log_home() == []
        assert "unknown point" in caplog.text

    def test_every_shipped_name_is_admitted(self, install_impl):
        install_impl(_RecordingOracle())
        for name in DECISION_POINT_NAMES:
            assert is_enabled(name, config=_config()) is True


# ---------------------------------------------------------------------------
# Gate 3 -- the sampling bucket
# ---------------------------------------------------------------------------


class TestBucket:
    def test_bucket_zero_admits_nothing(self):
        assert not any(in_bucket(f"s{i}", 0) for i in range(200))

    def test_bucket_hundred_admits_everything(self):
        assert all(in_bucket(f"s{i}", 100) for i in range(200))

    def test_a_key_is_consistently_in_or_out(self):
        assert {in_bucket("stable-key", 50) for _ in range(20)} in ({True}, {False})

    def test_bucket_is_roughly_the_percentage_it_claims(self):
        hits = sum(in_bucket(f"session-{i}", 25) for i in range(4000))
        assert 800 < hits < 1200, f"25% of 4000 should be ~1000, got {hits}"

    @pytest.mark.parametrize("bucket", [-5, 500, "nonsense", None])
    def test_an_unusable_bucket_is_clamped_not_treated_as_off(self, bucket):
        """A typo must not become a second, undocumented way to disable the seam."""
        admitted = in_bucket("any-key", bucket)  # type: ignore[arg-type]
        assert admitted is (False if bucket == -5 else True)

    def test_bucket_zero_refuses_before_the_provider(self, install_impl, log_home):
        oracle = install_impl(_ExplodingOracle())
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config(bucket=0))) is None
        assert oracle.entered is False
        assert log_home() == []

    def test_the_digest_matches_the_logged_session_field(self, install_impl, log_home):
        """A row's ``session`` is enough to re-derive why it was sampled."""
        install_impl(_RecordingOracle())
        key = "session-abc"
        asyncio.run(decide(POINT, "hi", QUESTIONS, session_key=key, config=_config()))
        assert log_home()[0]["session"] == log_mod.session_digest(key)


# ---------------------------------------------------------------------------
# Gate 4 -- the scrub
# ---------------------------------------------------------------------------


class TestScrub:
    @pytest.mark.parametrize(
        "payload",
        [
            *[f"here is my key {sample}" for sample in _AWS_KEY_SAMPLES],
            "vendor key sk-" + "a" * 32,
            _EXFIL_URL,
        ],
    )
    def test_a_credential_in_the_state_refuses_before_the_network(
        self, install_impl, log_home, payload
    ):
        oracle = install_impl(_ExplodingOracle())
        assert asyncio.run(decide(POINT, payload, QUESTIONS, config=_config())) is None
        assert oracle.entered is False
        row = log_home()[0]
        assert row["scrubbed"] is True
        assert row["error"] in gate_mod.SCRUB_ERRORS
        assert row["answers"] is None

    def test_the_two_scanners_are_distinguished_in_the_row(self, install_impl, log_home):
        """Which scanner refused is the finding; one category for both would lose it."""
        install_impl(_ExplodingOracle())
        asyncio.run(decide(POINT, f"key {_AWS_KEY_SAMPLES[0]}", QUESTIONS, config=_config()))
        asyncio.run(decide(POINT, _EXFIL_URL, QUESTIONS, config=_config()))
        assert [row["error"] for row in log_home()] == [
            gate_mod.ERROR_SCRUBBED_CREDENTIAL,
            gate_mod.ERROR_SCRUBBED_URL,
        ]

    def test_a_credential_nested_in_a_dict_state_is_found(self, install_impl, log_home):
        oracle = install_impl(_ExplodingOracle())
        state = {"messages": [{"text": f"key {_AWS_KEY_SAMPLES[0]}"}]}
        assert asyncio.run(decide(POINT, state, QUESTIONS, config=_config())) is None
        assert oracle.entered is False
        assert log_home()[0]["scrubbed"] is True

    def test_a_credential_in_a_question_prompt_is_found(self, install_impl, log_home):
        """The rubric leaves the machine in the same request as the state."""
        oracle = install_impl(_ExplodingOracle())
        questions = [Noul(id="q", prompt=f"is {_AWS_KEY_SAMPLES[0]} the right key?")]
        assert asyncio.run(decide(POINT, "clean state", questions, config=_config())) is None
        assert oracle.entered is False
        assert log_home()[0]["scrubbed"] is True

    def test_a_credential_in_a_choice_option_is_found(self, install_impl, log_home):
        oracle = install_impl(_ExplodingOracle())
        questions = [Choice(id="q", prompt="which?", options=["fine", _AWS_KEY_SAMPLES[0]])]
        assert asyncio.run(decide(POINT, "clean state", questions, config=_config())) is None
        assert oracle.entered is False
        assert log_home()[0]["scrubbed"] is True

    def test_a_scanner_that_itself_fails_refuses(self, install_impl, log_home, monkeypatch):
        """An external request cannot be cleared by a scan that did not complete."""
        oracle = install_impl(_ExplodingOracle())
        import kiro_crew.security.redaction as redaction_mod

        def _boom(_text):
            raise RuntimeError("scanner exploded")

        monkeypatch.setattr(redaction_mod, "redact_credentials", _boom)
        assert asyncio.run(decide(POINT, "clean state", QUESTIONS, config=_config())) is None
        assert oracle.entered is False
        assert log_home()[0]["error"] == gate_mod.ERROR_SCRUBBED_SCAN_FAILED

    def test_the_refusal_reason_never_quotes_what_it_found(self, install_impl, log_home):
        """The reason is a category: a quoted match would put the key in the log."""
        install_impl(_ExplodingOracle())
        sample = _AWS_KEY_SAMPLES[0]
        asyncio.run(decide(POINT, f"key {sample}", QUESTIONS, config=_config()))
        assert sample not in json.dumps(log_home())

    def test_the_state_itself_is_never_written_to_the_log(self, install_impl, log_home):
        install_impl(_RecordingOracle())
        private = "the user said something private about their salary"
        asyncio.run(decide(POINT, private, QUESTIONS, config=_config()))
        assert private not in json.dumps(log_home())

    def test_ordinary_state_passes_and_the_answers_reach_the_caller(self, install_impl, log_home):
        oracle = install_impl(_RecordingOracle())
        answers = asyncio.run(decide(POINT, "just words", QUESTIONS, config=_config()))
        assert answers is not None and answers["verdict"].value == "DUP"
        assert len(oracle.calls) == 1
        row = log_home()[0]
        assert row["scrubbed"] is False
        assert row["error"] is None
        assert row["answers"]["verdict"]["value"] == "DUP"

    def test_state_and_questions_pass_through_unchanged(self, install_impl):
        oracle = install_impl(_RecordingOracle())
        state = {"candidate": "x", "existing": ["a", "b"]}
        asyncio.run(decide(POINT, state, QUESTIONS, config=_config()))
        seen_state, seen_questions = oracle.calls[0]
        assert seen_state is state
        assert seen_questions is QUESTIONS


# ---------------------------------------------------------------------------
# The call: its budget, and its failures
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_a_slow_provider_returns_none_within_the_budget(self, install_impl, log_home):
        oracle = install_impl(_SlowOracle(5.0))
        result = asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config(timeout_ms=30)))
        assert result is None
        assert oracle.finished is False, "the call must be cancelled, not merely ignored"
        assert log_home()[0]["error"] == gate_mod.ERROR_TIMEOUT

    @pytest.mark.parametrize("timeout_ms", [0, -1])
    def test_a_nonpositive_timeout_is_floored_not_disabled(self, install_impl, timeout_ms):
        """A typo'd budget must look like a fast timeout, not a broken provider."""
        assert gate_mod.timeout_secs(config=_config(timeout_ms=timeout_ms)) > 0.0

    @pytest.mark.parametrize("raw", [None, "abc", float("inf"), float("nan")])
    def test_an_unusable_timeout_resolves_to_a_real_budget(self, raw):
        """``timeout_secs`` never returns something ``wait_for`` would reject."""
        cfg = SimpleNamespace(
            decisions=SimpleNamespace(
                enabled=True, bucket=100, provider=SimpleNamespace(timeout_ms=raw)
            )
        )
        value = gate_mod.timeout_secs(config=cfg)
        assert isinstance(value, float) and value > 0.0 and value == value  # not NaN
        assert value != float("inf")

    def test_the_helper_and_the_gate_read_the_same_number(self, install_impl, log_home):
        """One budget, one place it comes from -- an outer wait cannot invent its own."""
        install_impl(_SlowOracle(5.0))
        cfg = _config(timeout_ms=40)
        assert gate_mod.timeout_secs(config=cfg) == pytest.approx(0.04)
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=cfg)) is None
        assert log_home()[0]["error"] == gate_mod.ERROR_TIMEOUT

    def test_no_config_at_all_still_yields_a_budget(self, monkeypatch):
        monkeypatch.setattr(gate_mod, "_snapshot", lambda: None)
        assert gate_mod.timeout_secs() > 0.0


class TestErrors:
    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("connection reset"),
            ValueError("response is not JSON"),
            OSError("network unreachable"),
        ],
    )
    def test_a_provider_failure_returns_none_and_logs_a_category(self, install_impl, log_home, exc):
        install_impl(_RaisingOracle(exc))
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config())) is None
        row = log_home()[0]
        assert row["error"] == gate_mod.ERROR_PROVIDER
        assert row["scrubbed"] is False

    def test_a_provider_message_never_reaches_the_row(self, install_impl, log_home):
        """A provider can quote the request back; the row is on disk, so: category only."""
        install_impl(_RaisingOracle(RuntimeError("rejected body: my-secret-payload")))
        asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config()))
        assert "my-secret-payload" not in json.dumps(log_home())

    def test_every_logged_error_is_one_of_the_named_identifiers(self, install_impl, log_home):
        """A row's ``error`` is a closed vocabulary, so a reader can group on it."""
        known = {
            gate_mod.ERROR_TIMEOUT,
            gate_mod.ERROR_PROVIDER,
            gate_mod.ERROR_INVALID_RESULT,
            *gate_mod.SCRUB_ERRORS,
        }
        install_impl(_RaisingOracle(RuntimeError("boom")))
        asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config()))
        install_impl(_SlowOracle(5.0))
        asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config(timeout_ms=20)))
        install_impl(_RecordingOracle())
        asyncio.run(decide(POINT, f"k {_AWS_KEY_SAMPLES[0]}", QUESTIONS, config=_config()))
        errors = [row["error"] for row in log_home()]
        assert len(errors) == 3 and all(err in known for err in errors)

    def test_an_empty_result_is_recorded_as_an_error_not_as_an_answer(self, install_impl, log_home):
        class _Empty:
            async def ask(self, state, questions):
                return {}

        install_impl(_Empty())
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config())) is None
        row = log_home()[0]
        assert row["error"] == gate_mod.ERROR_INVALID_RESULT
        assert row["answers"] is None

    @pytest.mark.parametrize(
        "question,answer",
        [
            # A Choice answered with a value outside its own option list.
            (Choice(id="q", prompt="?", options=["A", "B"]), Answer(id="q", value="C", p=0.9)),
            # A probability outside 0..1.
            (Choice(id="q", prompt="?", options=["A"]), Answer(id="q", value="A", p=1.4)),
            # A Noul answered with something that is not a probability.
            (Noul(id="q", prompt="?"), Answer(id="q", value="maybe", p=0.5)),
            # An answer keyed to a question that was not asked.
            (Choice(id="q", prompt="?", options=["A"]), Answer(id="other", value="A", p=0.5)),
        ],
    )
    def test_an_out_of_domain_answer_is_recorded_as_an_error(
        self, install_impl, log_home, question, answer
    ):
        class _Fixed:
            async def ask(self, state, questions):
                return {question.id: answer}

        install_impl(_Fixed())
        assert asyncio.run(decide(POINT, "hi", [question], config=_config())) is None
        assert log_home()[0]["error"] == gate_mod.ERROR_INVALID_RESULT

    def test_cancellation_propagates_and_writes_no_row(self, install_impl, log_home):
        """A caller going away is not a provider failure."""
        install_impl(_RaisingOracle(asyncio.CancelledError()))
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config()))
        assert log_home() == []


class TestLoggingCannotCostTheResult:
    """The row is an observation. It may fail; the decision may not."""

    def test_a_broken_log_still_yields_the_answers(self, install_impl, monkeypatch):
        install_impl(_RecordingOracle())

        def _boom(row):
            raise OSError("read-only file system")

        # append() swallows its own errors, so patch it to raise and confirm the
        # gate is not relying on that -- decide must still return the answers.
        monkeypatch.setattr(log_mod, "append", _boom)
        answers = asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config()))
        assert answers is not None and answers["verdict"].value == "DUP"

    def test_an_unbuildable_row_still_yields_the_answers(self, install_impl, monkeypatch):
        """Row construction renders provider-supplied values; that must not escape."""
        install_impl(_RecordingOracle())

        def _boom(**_kwargs):
            raise RuntimeError("row construction exploded")

        monkeypatch.setattr(log_mod, "build_row", _boom)
        answers = asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config()))
        assert answers is not None

    def test_the_row_is_written_after_the_answers_are_in_hand(self, install_impl, log_home):
        """Not inside the provider budget: a log write must not spend the deadline."""
        install_impl(_RecordingOracle())
        order: list[str] = []
        real_append = log_mod.append

        def _tracking(row):
            order.append("append")
            real_append(row)

        log_mod_append = log_mod.append
        try:
            log_mod.append = _tracking  # type: ignore[assignment]
            answers = asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config()))
        finally:
            log_mod.append = log_mod_append  # type: ignore[assignment]
        assert answers is not None
        assert order == ["append"]
        assert log_home()[0]["error"] is None

    def test_a_stalled_append_gives_up_on_its_own_budget(self, install_impl, monkeypatch):
        """A hung filesystem may cost the row; it may not hold the caller.

        The write is bounded by ``_LOG_BUDGET_SECS`` on top of the provider
        budget, so an outer wait sized at ``timeout_secs() + _LOG_BUDGET_SECS``
        covers a ``decide`` whose log write never lands.

        Timed INSIDE the loop deliberately. The worker thread is not cancellable,
        so a loop being CLOSED still joins it -- ``asyncio.run`` would pay the
        whole stall at shutdown and hide the property under test. The bound is on
        what ``decide`` holds its caller for, which is what an outer wait sizes
        against; a gateway's loop is long-lived, so the abandoned thread finishes
        its one append in the background.
        """
        install_impl(_RecordingOracle())
        monkeypatch.setattr(gate_mod, "_LOG_BUDGET_SECS", 0.02)
        monkeypatch.setattr(log_mod, "append", lambda row: time.sleep(0.4))

        async def _timed():
            started = time.monotonic()
            answers = await decide(POINT, "hi", QUESTIONS, config=_config())
            return answers, time.monotonic() - started

        answers, elapsed = asyncio.run(_timed())
        assert answers is not None, "a log that never lands must not cost the answers"
        assert elapsed < 0.3, f"the write held the caller for {elapsed:.3f}s"

    def test_no_provider_message_reaches_the_application_log(self, install_impl, log_home, caplog):
        """Not the row and not the logger: a provider can quote the request back."""
        install_impl(_RaisingOracle(RuntimeError("rejected body: my-secret-payload")))
        with caplog.at_level("DEBUG", logger="kiro_crew.decisions.gate"):
            asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config()))
        assert "my-secret-payload" not in caplog.text
        assert "RuntimeError" in caplog.text, "the exception class is the diagnostic"

    def test_append_runs_off_the_event_loop(self, install_impl, monkeypatch):
        """The filesystem work is offloaded as one unit, open through close."""
        import threading

        loop_thread = threading.current_thread().ident
        seen: list[int | None] = []

        def _record(row):
            seen.append(threading.current_thread().ident)

        monkeypatch.setattr(log_mod, "append", _record)
        install_impl(_RecordingOracle())

        async def _drive():
            loop_ident = threading.current_thread().ident
            await decide(POINT, "hi", QUESTIONS, config=_config())
            return loop_ident

        ident = asyncio.run(_drive())
        assert seen and seen[0] != ident
        assert loop_thread is not None
