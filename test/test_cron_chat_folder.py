"""``chat_folder_id``: filing a scheduled job's RUNS into a chat folder.

A recurring job's output lands loose — in dashboard notifications, in a Slack
DM, or appended onto one long-lived tab — so there is no way to read a job's
runs back as a timeline. ``chat_folder_id`` names a folder in the chat
sidebar's own tree and every run of the job is filed there.

What these tests hold, and why each one can fail:

* **The field is its own field.** ``folder_id`` already exists on ``CronJob`` and
  means something else entirely (it groups the job's ROW on the Schedule page).
  The two must never be read off each other, so the persistence and REST tests
  assert one moves without the other.
* **Filing SUPPLEMENTS delivery.** The feature must not become a fourth delivery
  mode that replaces notification/Slack/origin delivery, so the delivery-shape
  tests assert the result row is written and the caller's return value is
  unchanged whether or not a folder is configured.
* **A deleted folder costs the placement and nothing else.** A folder can be
  deleted while a job still names it. The run must still deliver, its session
  must land unfiled, and the skip must be recorded once.
* **A rename is a no-op.** The job stores the folder's id, so there is nothing to
  do — pinned so a future "resolve by name" shortcut cannot be added silently.
* **Back-compat.** A job with no ``chat_folder_id`` behaves exactly as it did:
  the mock state's ``_folders`` is empty in those tests, which would surface any
  accidental unconditional folder lookup as an exception rather than a pass.

The state fake mirrors ``test_cron_first_run_tab.py``'s, including the reason its
``get_slot``/``has_slot`` are REAL functions over one dict: on a bare
``MagicMock`` both auto-return truthy mocks, which would satisfy every
short-circuit under test and make the assertions unable to fail.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronJob, CronService
from kiro_crew.dashboard.chat_utils import cron_slot_name
from kiro_crew.dashboard.cron_inject import (
    chat_folder_exists,
    cron_run_gets_tab,
    cron_suppressed_run_gets_tab,
    deliver_cron_run,
    file_cron_run_in_chat_folder,
    inject_cron_result_to_dashboard,
    per_run_tab_would_exceed_the_slot_ceiling,
    prefetch_cron_run_history,
    unfile_cron_job_tab,
)
from kiro_crew.dashboard.handlers.cron import api_cron_update, api_crons, api_crons_create
from kiro_crew.dashboard.state import MAX_LIVE_SLOTS
from kiro_crew.session_surface import has_dashboard_surface, set_dashboard_surfaced

FOLDER = "f00dcafe"
OTHER_FOLDER = "beadfeed"


@pytest.fixture(autouse=True)
def _reset_surface_registry():
    """The bind publishes into the process-global dashboard-surface registry."""
    set_dashboard_surfaced(())
    yield
    set_dashboard_surfaced(())


def _make_state(folders=(FOLDER,), history_messages=None):
    """A mock DashboardState whose slot accessors are real functions."""
    state = MagicMock()
    slots: dict[str, MagicMock] = {}
    state._slots = slots
    state._folders = [{"id": fid, "name": f"folder-{fid}"} for fid in folders]

    def get_or_create_slot(name=None, agent="", origin=""):
        if name not in slots:
            slot = MagicMock()
            slot.key = name
            slot._origin = origin
            slot.linked_session_key = ""
            slot.messages = []
            slot.title = ""
            slot.folder_id = ""

            def append(role, content, cls, broadcast=True, meta=None, mint_mid=True):
                supplied = meta.get("mid") if isinstance(meta, dict) else None
                stored_meta = dict(meta) if isinstance(meta, dict) else {}
                if mint_mid and not supplied:
                    stored_meta["mid"] = f"m-test-{len(slot.messages)}"
                msg = {
                    "role": role,
                    "content": content,
                    "cls": cls,
                    **({"meta": stored_meta} if stored_meta else {}),
                }
                slot.messages.append(msg)
                return msg

            slot.append = append
            slots[name] = slot
        return slots[name]

    state.get_or_create_slot = get_or_create_slot
    state.get_slot = lambda name: slots.get(name)
    state.has_slot = lambda name: name in slots
    # A REAL function over the same dict: on a bare MagicMock this returns a mock,
    # and `mock >= 500` raises rather than answering, so the ceiling branch would
    # never be exercised either way.
    state.live_slot_count = lambda: len(slots)
    state.conversation_log = MagicMock()
    state.conversation_log.read_messages.return_value = history_messages or []
    state.push_slots_update = MagicMock()
    return state


def _job(**over) -> CronJob:
    fields = {"id": "job42", "name": "standup brief", "message": "Write the brief."}
    fields.update(over)
    job = CronJob(**fields)
    job.set_run_result("the brief")
    return job


# ---------------------------------------------------------------------------
# 1. Persistence: the field survives a save/load round trip on its own
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_the_field_round_trips_through_the_store(self, tmp_path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("brief", "Write it.", every_secs=3600, chat_folder_id=FOLDER)

        reloaded = CronService(base_dir=tmp_path)
        found = next(j for j in reloaded.list_jobs(include_disabled=True) if j.id == job.id)
        assert found.chat_folder_id == FOLDER

    def test_it_is_written_to_disk_under_its_own_key(self, tmp_path) -> None:
        """Not folded into ``folder_id``: the two name folders in different trees."""
        svc = CronService(base_dir=tmp_path)
        svc.add_job("brief", "Write it.", every_secs=3600, chat_folder_id=FOLDER)

        record = json.loads((tmp_path / "crons.json").read_text())["jobs"][0]
        assert record["chat_folder_id"] == FOLDER
        assert record["folder_id"] == ""

    def test_an_absent_key_reads_as_not_filed(self, tmp_path) -> None:
        """A store written before the field existed keeps working."""
        (tmp_path / "crons.json").write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": "legacy01",
                            "name": "old",
                            "message": "go",
                            "schedule": {"kind": "every", "every_secs": 60},
                        }
                    ]
                }
            )
        )
        svc = CronService(base_dir=tmp_path)
        assert svc.list_jobs(include_disabled=True)[0].chat_folder_id == ""

    def test_update_moves_it_without_touching_the_schedule_folder(self, tmp_path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(
            "brief", "Write it.", every_secs=3600, folder_id="sched1", chat_folder_id=FOLDER
        )
        svc.update_job(job.id, chat_folder_id=OTHER_FOLDER)

        moved = next(j for j in svc.list_jobs(include_disabled=True) if j.id == job.id)
        assert moved.chat_folder_id == OTHER_FOLDER
        assert moved.folder_id == "sched1"

    def test_update_clears_it_with_an_empty_value(self, tmp_path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("brief", "Write it.", every_secs=3600, chat_folder_id=FOLDER)
        svc.update_job(job.id, chat_folder_id="")
        cleared = next(j for j in svc.list_jobs(include_disabled=True) if j.id == job.id)
        assert cleared.chat_folder_id == ""

    def test_the_store_refuses_a_non_string(self, tmp_path) -> None:
        """The table-driven chokepoint gate covers the new field like its siblings."""
        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError):
            svc.add_job("brief", "Write it.", every_secs=3600, chat_folder_id=["nope"])

    def test_the_store_refuses_an_over_cap_string(self, tmp_path) -> None:
        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError):
            svc.add_job("brief", "Write it.", every_secs=3600, chat_folder_id="x" * 5000)


# ---------------------------------------------------------------------------
# 2. The delivery predicates
# ---------------------------------------------------------------------------


class TestWhichRunsGetATab:
    def test_a_persistent_job_still_gets_one(self) -> None:
        assert cron_run_gets_tab(_job()) is True

    def test_a_stateless_job_without_a_folder_still_gets_none(self) -> None:
        """Unchanged behaviour: ``persistent_session=False`` means no tab."""
        assert cron_run_gets_tab(_job(persistent_session=False)) is False

    def test_a_stateless_job_with_a_folder_gets_one(self) -> None:
        assert cron_run_gets_tab(_job(persistent_session=False, chat_folder_id=FOLDER)) is True

    def test_hide_in_chat_wins_over_a_configured_folder(self) -> None:
        """The narrower, older and more emphatic opt-out resolves the conflict."""
        job = _job(persistent_session=False, chat_folder_id=FOLDER, hide_in_chat=True)
        assert cron_run_gets_tab(job) is False
        assert cron_run_gets_tab(_job(chat_folder_id=FOLDER, hide_in_chat=True)) is False

    def test_a_suppressed_persistent_run_still_needs_an_existing_tab(self) -> None:
        state = _make_state()
        assert cron_suppressed_run_gets_tab(state, _job(chat_folder_id=FOLDER)) is False

    def test_a_suppressed_stateless_run_with_a_folder_gets_its_own_tab(self) -> None:
        """Its tab is per-run, so "already exists" is never true and the folder
        would silently lose every deduped or silent run."""
        state = _make_state()
        job = _job(persistent_session=False, chat_folder_id=FOLDER)
        assert cron_suppressed_run_gets_tab(state, job) is True


class TestFolderExistence:
    def test_a_known_id_exists(self) -> None:
        assert chat_folder_exists(_make_state(), FOLDER) is True

    def test_an_unknown_id_does_not(self) -> None:
        assert chat_folder_exists(_make_state(), OTHER_FOLDER) is False

    def test_an_empty_id_is_never_a_folder(self) -> None:
        assert chat_folder_exists(_make_state(), "") is False


# ---------------------------------------------------------------------------
# 3. Filing: the persistent tab, the per-run tab, and the deleted folder
# ---------------------------------------------------------------------------


class TestFilingThePersistentTab:
    def test_the_job_wide_tab_is_filed_into_the_folder(self) -> None:
        state = _make_state()
        job = _job(chat_folder_id=FOLDER)
        slot = inject_cron_result_to_dashboard(state, job, "the brief", history=[])

        assert file_cron_run_in_chat_folder(state, job, slot) is True
        assert slot.folder_id == FOLDER

    def test_a_job_with_no_folder_leaves_the_tab_unfiled(self) -> None:
        state = _make_state(folders=())
        job = _job()
        slot = inject_cron_result_to_dashboard(state, job, "the brief", history=[])

        assert file_cron_run_in_chat_folder(state, job, slot) is False
        assert slot.folder_id == ""

    def test_filing_is_idempotent_across_runs(self) -> None:
        """Every run calls this; only the first one moves anything, so a re-file
        cannot cost a metadata write per delivery."""
        state = _make_state()
        job = _job(chat_folder_id=FOLDER)
        slot = inject_cron_result_to_dashboard(state, job, "the brief", history=[])
        assert file_cron_run_in_chat_folder(state, job, slot) is True
        assert file_cron_run_in_chat_folder(state, job, slot) is False

    def test_a_rename_needs_no_work_because_the_id_is_stored(self) -> None:
        state = _make_state()
        job = _job(chat_folder_id=FOLDER)
        slot = inject_cron_result_to_dashboard(state, job, "the brief", history=[])
        file_cron_run_in_chat_folder(state, job, slot)

        state._folders[0]["name"] = "Renamed entirely"
        assert file_cron_run_in_chat_folder(state, job, slot) is False
        assert slot.folder_id == FOLDER


class TestFilingPerRunTabs:
    def test_a_stateless_run_gets_a_tab_of_its_own(self) -> None:
        state = _make_state()
        job = _job(persistent_session=False, chat_folder_id=FOLDER)

        slot = inject_cron_result_to_dashboard(
            state, job, "run one", history=None, run_session_key="cron:job42:aaaa1111"
        )
        assert slot.key == "cron-job42-aaaa1111"
        assert slot.linked_session_key == "cron:job42:aaaa1111"

    def test_two_runs_do_not_share_a_tab(self) -> None:
        """The whole point of the folder: a browsable entry per run, not one pile."""
        state = _make_state()
        job = _job(persistent_session=False, chat_folder_id=FOLDER)

        first = inject_cron_result_to_dashboard(
            state, job, "run one", history=None, run_session_key="cron:job42:aaaa1111"
        )
        second = inject_cron_result_to_dashboard(
            state, job, "run two", history=None, run_session_key="cron:job42:bbbb2222"
        )
        assert first.key != second.key
        assert "run one" in first.messages[-1]["content"]
        assert "run two" in second.messages[-1]["content"]
        assert all("run two" not in m["content"] for m in first.messages)

    def test_the_run_transcript_is_written_under_the_run_key(self) -> None:
        """Not under ``cron:{id}``: a per-run tab that replayed the job-wide pile
        would put back exactly the context ``persistent_session=False`` removes."""
        state = _make_state()
        job = _job(persistent_session=False, chat_folder_id=FOLDER)

        import kiro_crew.dashboard.cron_inject as mod

        written: list[str] = []
        original = mod.append_rows_if_absent_off_loop
        mod.append_rows_if_absent_off_loop = lambda log, key, rows, agent=None: written.append(key)
        try:
            inject_cron_result_to_dashboard(
                state, job, "run one", history=None, run_session_key="cron:job42:aaaa1111"
            )
        finally:
            mod.append_rows_if_absent_off_loop = original
        assert written == ["cron:job42:aaaa1111"]

    def test_a_per_run_tab_is_titled_with_the_run_stamp(self) -> None:
        """Its siblings in the folder all carry the same job name."""
        state = _make_state()
        job = _job(persistent_session=False, chat_folder_id=FOLDER)
        slot = inject_cron_result_to_dashboard(
            state, job, "run one", history=None, run_session_key="cron:job42:aaaa1111"
        )
        assert slot.title.startswith("Cron: standup brief")
        assert slot.title != "Cron: standup brief"

    def test_the_job_wide_tab_title_is_unchanged(self) -> None:
        """A stamp there would rewrite the title on every delivery."""
        state = _make_state()
        slot = inject_cron_result_to_dashboard(state, _job(), "the brief", history=[])
        assert slot.title == "Cron: standup brief"

    def test_the_per_run_tab_is_addressable_as_a_dashboard_surface(self) -> None:
        """Cards, notices and sub-agent events route by ``dashboard_slot_key``."""
        from kiro_crew.dashboard.chat_utils import dashboard_slot_key

        state = _make_state()
        job = _job(persistent_session=False, chat_folder_id=FOLDER)
        inject_cron_result_to_dashboard(
            state, job, "run one", history=None, run_session_key="cron:job42:aaaa1111"
        )
        assert has_dashboard_surface("cron:job42:aaaa1111")
        assert dashboard_slot_key("cron:job42:aaaa1111") == "cron-job42-aaaa1111"

    def test_a_sequential_agent_key_still_resolves_to_the_job_wide_tab(self) -> None:
        """``cron:<id>:<agent>`` shares the per-run SHAPE but is durable, and only
        the job's linked key is ever published for it."""
        from kiro_crew.dashboard.chat_utils import dashboard_slot_key

        state = _make_state()
        inject_cron_result_to_dashboard(state, _job(), "the brief", history=[])
        assert dashboard_slot_key("cron:job42:researcher") == "cron-job42"


class TestDeletedFolder:
    def test_the_run_still_delivers_and_lands_unfiled(self) -> None:
        state = _make_state(folders=())  # the folder was deleted after the save
        job = _job(chat_folder_id=FOLDER)

        slot = inject_cron_result_to_dashboard(state, job, "the brief", history=[])

        assert file_cron_run_in_chat_folder(state, job, slot) is False
        assert slot.folder_id == ""
        # The result is still there — the placement is the only thing lost.
        assert any("the brief" in m["content"] for m in slot.messages)

    def test_the_skip_is_recorded_once(self, monkeypatch) -> None:
        """ "Why is this run not in my folder?" must have an answer with no repro."""
        import kiro_crew.dashboard.cron_inject as mod

        logged: list[dict] = []
        monkeypatch.setattr(
            mod,
            "sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **kw: logged.append(kw)),
        )
        state = _make_state(folders=())
        job = _job(chat_folder_id=FOLDER)
        slot = inject_cron_result_to_dashboard(state, job, "the brief", history=[])

        file_cron_run_in_chat_folder(state, job, slot)
        assert [row["tool_name"] for row in logged] == ["cron_chat_folder_missing"]
        assert logged[0]["outcome"] == "skipped"

    def test_a_sel_failure_cannot_fail_the_run(self, monkeypatch) -> None:
        import kiro_crew.dashboard.cron_inject as mod

        def _boom():
            raise RuntimeError("no audit today")

        monkeypatch.setattr(mod, "sel", _boom)
        state = _make_state(folders=())
        job = _job(chat_folder_id=FOLDER)
        slot = inject_cron_result_to_dashboard(state, job, "the brief", history=[])
        assert file_cron_run_in_chat_folder(state, job, slot) is False


# ---------------------------------------------------------------------------
# 4. Supplements, never replaces
# ---------------------------------------------------------------------------


class TestSupplementsDelivery:
    @pytest.mark.asyncio
    async def test_the_result_row_is_written_whether_or_not_a_folder_is_set(self) -> None:
        filed_state, plain_state = _make_state(), _make_state(folders=())

        filed = await deliver_cron_run(
            filed_state, _job(chat_folder_id=FOLDER), "the brief", history=[]
        )
        plain = await deliver_cron_run(plain_state, _job(), "the brief", history=[])

        assert [m["role"] for m in filed.messages] == [m["role"] for m in plain.messages]
        assert filed.folder_id == FOLDER
        assert plain.folder_id == ""

    @pytest.mark.asyncio
    async def test_the_placement_is_persisted_so_the_folder_survives_a_restart(
        self, monkeypatch
    ) -> None:
        """The folder's timeline is read from the on-disk session list, so an
        in-memory-only assignment shows nothing after a restart."""
        import kiro_crew.dashboard.chat_persistence as persistence

        saved: list[tuple[str, bool]] = []

        async def _save(state, slot, *a, **kw):
            saved.append((slot.key, bool(kw.get("force"))))
            return True

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        await deliver_cron_run(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        assert saved == [("cron-job42", True)]

    @pytest.mark.asyncio
    async def test_no_metadata_write_when_nothing_moved(self, monkeypatch) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        saved: list[str] = []

        async def _save(state, slot, *a, **kw):
            saved.append(slot.key)
            return True

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state(folders=())
        await deliver_cron_run(state, _job(), "the brief", history=[])
        assert saved == []

    @pytest.mark.asyncio
    async def test_a_failed_persist_cannot_fail_a_completed_run(self, monkeypatch) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        async def _save(state, slot, *a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        slot = await deliver_cron_run(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        assert any("the brief" in m["content"] for m in slot.messages)


class TestRunHistoryPrefetch:
    @pytest.mark.asyncio
    async def test_a_persistent_job_still_hydrates_from_its_transcript(self) -> None:
        state = _make_state(history_messages=[{"role": "user", "content": "older run"}])
        assert await prefetch_cron_run_history(state, _job()) == [
            {"role": "user", "content": "older run"}
        ]

    @pytest.mark.asyncio
    async def test_a_stateless_job_reads_nothing(self) -> None:
        """Hydrating its per-run tab would put back the context the setting removes,
        and the skip also avoids a whole-file read the tab has no use for."""
        state = _make_state(history_messages=[{"role": "user", "content": "older run"}])
        job = _job(persistent_session=False, chat_folder_id=FOLDER)
        assert await prefetch_cron_run_history(state, job) is None
        state.conversation_log.read_messages.assert_not_called()


class TestSlotName:
    def test_the_job_wide_key_keeps_its_historical_spelling(self) -> None:
        assert cron_slot_name("cron:job42") == "cron-job42"

    def test_a_per_run_key_folds_its_extra_segment_with_a_hyphen(self) -> None:
        assert cron_slot_name("cron:job42:aaaa1111") == "cron-job42-aaaa1111"


class TestTheLiveSlotCeiling:
    """A per-run tab is the one cron surface that mints a slot per FIRE, so it is
    the one that can walk a short-interval job into the global slot ceiling.
    ``_bind_cron_slot`` reaches ``get_or_create_slot`` below the check
    ``session_control.create_session`` makes, so the bound has to be asked for in
    the bind or it is not asked at all."""

    def test_the_ceiling_is_read_off_the_live_slot_count(self) -> None:
        state = _make_state()
        assert per_run_tab_would_exceed_the_slot_ceiling(state) is False
        state.live_slot_count = lambda: MAX_LIVE_SLOTS
        assert per_run_tab_would_exceed_the_slot_ceiling(state) is True

    def test_a_new_per_run_tab_is_skipped_at_the_ceiling(self) -> None:
        state = _make_state()
        state.live_slot_count = lambda: MAX_LIVE_SLOTS
        job = _job(persistent_session=False, chat_folder_id=FOLDER)

        assert (
            inject_cron_result_to_dashboard(
                state, job, "run one", history=None, run_session_key="cron:job42:aaaa1111"
            )
            is None
        )
        # Nothing written: a half-filed session in the folder would be worse than none.
        assert state._slots == {}

    def test_the_skip_is_recorded_once(self, monkeypatch) -> None:
        import kiro_crew.dashboard.cron_inject as mod

        logged: list[dict] = []
        monkeypatch.setattr(
            mod,
            "sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **kw: logged.append(kw)),
        )
        state = _make_state()
        state.live_slot_count = lambda: MAX_LIVE_SLOTS
        inject_cron_result_to_dashboard(
            state,
            _job(persistent_session=False, chat_folder_id=FOLDER),
            "run one",
            history=None,
            run_session_key="cron:job42:aaaa1111",
        )
        assert [row["tool_name"] for row in logged] == ["cron_per_run_tab_skipped"]

    def test_the_job_wide_tab_is_never_refused(self) -> None:
        """It is created once and reused for the job's whole life, so refusing it
        at the ceiling would silence an established job for an unrelated reason."""
        state = _make_state()
        state.live_slot_count = lambda: MAX_LIVE_SLOTS + 10
        slot = inject_cron_result_to_dashboard(
            state, _job(chat_folder_id=FOLDER), "brief", history=[]
        )
        assert slot is not None
        assert slot.key == "cron-job42"

    def test_an_existing_per_run_tab_is_still_written_to(self) -> None:
        """The bound is on MINTING a slot. A tab that already exists costs nothing
        new, so a re-delivery onto it must not be refused."""
        state = _make_state()
        job = _job(persistent_session=False, chat_folder_id=FOLDER)
        first = inject_cron_result_to_dashboard(
            state, job, "run one", history=None, run_session_key="cron:job42:aaaa1111"
        )
        assert first is not None
        state.live_slot_count = lambda: MAX_LIVE_SLOTS
        again = inject_cron_result_to_dashboard(
            state, job, "run one again", history=None, run_session_key="cron:job42:aaaa1111"
        )
        assert again is first

    @pytest.mark.asyncio
    async def test_the_delivery_chokepoint_files_nothing_on_a_skip(self, monkeypatch) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        saved: list[str] = []

        async def _save(state, slot, *a, **kw):
            saved.append(slot.key)
            return True

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        state.live_slot_count = lambda: MAX_LIVE_SLOTS
        result = await deliver_cron_run(
            state,
            _job(persistent_session=False, chat_folder_id=FOLDER),
            "run one",
            history=None,
            run_session_key="cron:job42:aaaa1111",
        )
        assert result is None
        assert saved == []


class TestClearingTheField:
    """Clearing the picker has to take the job's tab OUT of the folder, and that
    happens at the save. At delivery the two states are indistinguishable -- a job
    with no folder whose tab sits in one looks identical whether this feature put
    it there or the reader dragged it there -- so unfiling on that guess would move
    a session the reader placed themselves."""

    @pytest.mark.asyncio
    async def test_the_job_wide_tab_is_unfiled(self, monkeypatch) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        saved: list[tuple[str, bool]] = []

        async def _save(state, slot, *a, **kw):
            saved.append((slot.key, bool(kw.get("force"))))
            return True

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        job = _job(chat_folder_id=FOLDER)
        slot = inject_cron_result_to_dashboard(state, job, "brief", history=[])
        file_cron_run_in_chat_folder(state, job, slot)
        assert slot.folder_id == FOLDER

        await unfile_cron_job_tab(state, _job())
        assert slot.folder_id == ""
        assert saved == [("cron-job42", True)]

    @pytest.mark.asyncio
    async def test_an_unfiled_tab_needs_no_write(self, monkeypatch) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        saved: list[str] = []

        async def _save(state, slot, *a, **kw):
            saved.append(slot.key)
            return True

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        inject_cron_result_to_dashboard(state, _job(), "brief", history=[])
        await unfile_cron_job_tab(state, _job())
        assert saved == []

    @pytest.mark.asyncio
    async def test_a_job_with_no_tab_at_all_is_a_no_op(self) -> None:
        await unfile_cron_job_tab(_make_state(), _job())

    @pytest.mark.asyncio
    async def test_per_run_tabs_keep_their_placement(self, monkeypatch) -> None:
        """They are history. Today's settings change is no reason to rewrite where
        last week's runs are filed."""
        import kiro_crew.dashboard.chat_persistence as persistence

        monkeypatch.setattr(persistence, "save_slot_off_loop", AsyncMock(return_value=True))
        state = _make_state()
        job = _job(persistent_session=False, chat_folder_id=FOLDER)
        run = await deliver_cron_run(
            state, job, "run one", history=None, run_session_key="cron:job42:aaaa1111"
        )
        assert run is not None and run.folder_id == FOLDER

        await unfile_cron_job_tab(state, _job(persistent_session=False))
        assert run.folder_id == FOLDER

    @pytest.mark.asyncio
    async def test_a_failed_persist_cannot_fail_the_save(self, monkeypatch) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        async def _save(state, slot, *a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        job = _job(chat_folder_id=FOLDER)
        slot = inject_cron_result_to_dashboard(state, job, "brief", history=[])
        file_cron_run_in_chat_folder(state, job, slot)
        await unfile_cron_job_tab(state, _job())
        assert slot.folder_id == ""


# ---------------------------------------------------------------------------
# 5. The REST surface
# ---------------------------------------------------------------------------


def _app(handler, route: str, *, folders=(FOLDER,), **store) -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(
        crons=SimpleNamespace(**store),
        _folders=[{"id": fid, "name": f"folder-{fid}"} for fid in folders],
        push_refresh=MagicMock(),
        ack_notification=AsyncMock(),
        has_slot=MagicMock(return_value=False),
        # The clear path asks for the job's own tab; no tab is the ordinary case
        # for a handler test, and a REAL function keeps it from answering a mock.
        get_slot=lambda name: None,
        push_slots_update=MagicMock(),
    )
    app.router.add_route("*", route, handler)
    return app


_BODY = {"name": "brief", "message": "Write it.", "every": 3600}


@pytest.mark.asyncio
class TestRestCreate:
    async def test_the_field_reaches_the_store(self) -> None:
        add = AsyncMock(return_value=_job(chat_folder_id=FOLDER))
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/crons", json={**_BODY, "chat_folder_id": FOLDER})
            assert resp.status == 200
        assert add.await_args.kwargs["chat_folder_id"] == FOLDER

    async def test_an_absent_field_creates_an_unfiled_job(self) -> None:
        add = AsyncMock(return_value=_job())
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            assert (await client.post("/api/crons", json=_BODY)).status == 200
        assert add.await_args.kwargs["chat_folder_id"] == ""

    async def test_an_unknown_folder_is_refused_at_save_time(self) -> None:
        """A save is the one moment a person is present to be told."""
        add = AsyncMock(return_value=_job())
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/crons", json={**_BODY, "chat_folder_id": "nope1234"})
            assert resp.status == 400
            assert (await resp.json())["code"] == "unknown_chat_folder"
        add.assert_not_awaited()

    async def test_a_non_string_is_refused(self) -> None:
        add = AsyncMock(return_value=_job())
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/crons", json={**_BODY, "chat_folder_id": 7})
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_chat_folder_id"

    async def test_null_means_unfiled_rather_than_a_refusal(self) -> None:
        add = AsyncMock(return_value=_job())
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/crons", json={**_BODY, "chat_folder_id": None})
            assert resp.status == 200
        assert add.await_args.kwargs["chat_folder_id"] == ""

    async def test_the_schedule_folder_is_unaffected(self) -> None:
        add = AsyncMock(return_value=_job())
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/api/crons", json={**_BODY, "chat_folder_id": FOLDER, "folder_id": "sched9"}
            )
        assert add.await_args.kwargs["folder_id"] == "sched9"
        assert add.await_args.kwargs["chat_folder_id"] == FOLDER


@pytest.mark.asyncio
class TestRestUpdate:
    async def test_the_field_reaches_the_store(self) -> None:
        update = AsyncMock(return_value=_job(chat_folder_id=FOLDER))
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/job42", json={"chat_folder_id": FOLDER})
            assert resp.status == 200
        assert update.await_args.kwargs["chat_folder_id"] == FOLDER

    async def test_an_unknown_folder_is_refused(self) -> None:
        update = AsyncMock(return_value=_job())
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/job42", json={"chat_folder_id": "nope1234"})
            assert resp.status == 400
            assert (await resp.json())["code"] == "unknown_chat_folder"
        update.assert_not_awaited()

    async def test_null_clears_the_field(self) -> None:
        update = AsyncMock(return_value=_job())
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/job42", json={"chat_folder_id": None})
            assert resp.status == 200
        assert update.await_args.kwargs["chat_folder_id"] == ""

    async def test_clearing_it_unfiles_the_job_tab(self, monkeypatch) -> None:
        """The clear is applied where the intent is unambiguous."""
        import kiro_crew.dashboard.handlers.cron as handler_mod

        unfiled: list[str] = []

        async def _unfile(state, job):
            unfiled.append(job.id)

        monkeypatch.setattr(handler_mod, "unfile_cron_job_tab", _unfile)
        update = AsyncMock(return_value=_job())
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/job42", json={"chat_folder_id": ""})
            assert resp.status == 200
        assert unfiled == ["job42"]

    async def test_setting_a_folder_does_not_unfile(self, monkeypatch) -> None:
        import kiro_crew.dashboard.handlers.cron as handler_mod

        unfiled: list[str] = []

        async def _unfile(state, job):
            unfiled.append(job.id)

        monkeypatch.setattr(handler_mod, "unfile_cron_job_tab", _unfile)
        update = AsyncMock(return_value=_job(chat_folder_id=FOLDER))
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            await client.patch("/api/crons/job42", json={"chat_folder_id": FOLDER})
        assert unfiled == []

    async def test_an_untouched_field_does_not_unfile(self, monkeypatch) -> None:
        """A rename must not move the job's tab."""
        import kiro_crew.dashboard.handlers.cron as handler_mod

        unfiled: list[str] = []

        async def _unfile(state, job):
            unfiled.append(job.id)

        monkeypatch.setattr(handler_mod, "unfile_cron_job_tab", _unfile)
        update = AsyncMock(return_value=_job(chat_folder_id=FOLDER))
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            await client.patch("/api/crons/job42", json={"name": "renamed"})
        assert unfiled == []

    async def test_an_untouched_field_is_not_sent_to_the_store(self) -> None:
        """Editing any other setting must not rewrite the placement."""
        update = AsyncMock(return_value=_job(chat_folder_id=FOLDER))
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            await client.patch("/api/crons/job42", json={"silent": True})
        assert "chat_folder_id" not in update.await_args.kwargs


@pytest.mark.asyncio
class TestRestList:
    async def test_the_payload_carries_the_field(self) -> None:
        """Without it the form control defaults on load and the next save of any
        unrelated change silently unfiles the job."""
        job = _job(chat_folder_id=FOLDER)
        state = MagicMock()
        state.has_slot.return_value = False
        state.crons.list_jobs.return_value = [job]
        state.crons.list_jobs_async = AsyncMock(return_value=[job])
        state.crons.is_running.return_value = False
        state.crons.running_since.return_value = None
        request = MagicMock()
        request.app = {"state": state}

        rows = json.loads((await api_crons(request)).body)["jobs"]
        assert rows[0]["chat_folder_id"] == FOLDER
        assert rows[0]["folder_id"] == ""
