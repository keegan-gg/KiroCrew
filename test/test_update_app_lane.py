"""The packaged-desktop-app update lane: bridge, arm, approve.

What these pin is the privilege split, not the plumbing. An agent may ARM an
update on a packaged install; only a host-identity approval (reading the
trust-fenced nonce) may turn that into an install; and the install itself is
performed by the desktop process that owns the bytes, reached through a queue it
PULLS from rather than any inbound control surface on it.

The refusals matter as much as the successes, so each one has its own test: no
desktop host polling, no version discovered, a minimum-version floor, a lane
that no longer matches the install's shape, and an approve with no arm at all.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.handlers import updates
from kiro_crew.platform import app_update_bridge, update_stepup
from kiro_crew.platform.update_capability import (
    MANAGED_BY_ELECTRON,
    MANAGED_BY_KIROCREW,
    MODE_CONSENT,
    UNAVAILABLE_MANAGED_BY_APP,
    UpdateCapability,
)


def _request(
    body: object = None,
    *,
    remote: str = "127.0.0.1",
    marks: dict[str, object] | None = None,
) -> MagicMock:
    """A request double shaped like the one in test_update_stepup.

    ``get`` is a real mapping lookup: a bare MagicMock returns a truthy mock for
    every key, which would make an auth-mark gate pass vacuously and hide
    exactly the boundary these tests exist to prove.
    """
    req = MagicMock()

    async def _json() -> object:
        if isinstance(body, Exception):
            raise body
        return body

    req.json = _json
    req.remote = remote
    store = dict(marks or {})
    req.get = store.get
    req.__contains__ = lambda self, key: key in store
    req.transport.get_extra_info = lambda key, default=None: default
    state = MagicMock()
    state._background_tasks = set()
    req.app = {"state": state}
    return req


def _electron_capability() -> UpdateCapability:
    """What ``derive_capability`` returns for a packaged desktop install."""
    return UpdateCapability(
        supported=True,
        managed_by=MANAGED_BY_ELECTRON,
        mode=MODE_CONSENT,
        can_download=False,
        can_apply=False,
        requires_restart=True,
        unavailable_reason=UNAVAILABLE_MANAGED_BY_APP,
    )


def _wheel_capability() -> UpdateCapability:
    return UpdateCapability(
        supported=True,
        managed_by=MANAGED_BY_KIROCREW,
        mode=MODE_CONSENT,
        can_download=True,
        can_apply=False,
        requires_restart=True,
    )


@pytest.fixture()
def packaged(monkeypatch: pytest.MonkeyPatch):
    """A packaged install with a desktop host reporting an available update."""
    from kiro_crew.platform import update_capability

    monkeypatch.setattr(update_capability, "derive_capability", _electron_capability)
    monkeypatch.setattr(updates, "resolve_provider", lambda: None)
    bridge = app_update_bridge.get_app_update_bridge()
    bridge.clear()
    bridge.report_state(version="0.5.0", available_version="0.6.0", channel="stable")
    try:
        yield bridge
    finally:
        bridge.clear()
        update_stepup.clear_pending()


class TestBridgeModule:
    def test_state_expires_so_a_departed_host_stops_answering(self) -> None:
        clock = {"t": 1000.0}
        bridge = app_update_bridge.AppUpdateBridge(now=lambda: clock["t"])
        bridge.report_state(version="1", available_version="2", channel="stable")
        assert bridge.host_present() is True
        clock["t"] += app_update_bridge.HOST_TTL_S + 1
        assert bridge.host_state() is None
        assert bridge.host_present() is False

    def test_install_request_is_delivered_exactly_once(self) -> None:
        bridge = app_update_bridge.AppUpdateBridge()
        bridge.request_install(version="0.6.0", channel="stable", request_id="abc")
        taken = bridge.take_install()
        assert taken is not None and taken.version == "0.6.0"
        # Popped, not marked: a crashed host must not be handed the same quit
        # again on its next launch, long after the human approved it.
        assert bridge.take_install() is None

    def test_stale_install_request_is_dropped_rather_than_delivered_late(self) -> None:
        clock = {"t": 0.0}
        bridge = app_update_bridge.AppUpdateBridge(now=lambda: clock["t"])
        bridge.request_install(version="0.6.0", channel="stable", request_id="abc")
        clock["t"] += app_update_bridge.INSTALL_TTL_S + 1
        assert bridge.pending_install() is None
        assert bridge.take_install() is None

    def test_second_approval_supersedes_the_first(self) -> None:
        bridge = app_update_bridge.AppUpdateBridge()
        bridge.request_install(version="0.6.0", channel="stable", request_id="one")
        bridge.request_install(version="0.7.0", channel="stable", request_id="two")
        taken = bridge.take_install()
        assert taken is not None and taken.request_id == "two"
        assert bridge.take_install() is None


class TestStepUpLane:
    def test_lane_round_trips_through_the_nonce_file(self) -> None:
        update_stepup.arm("0.6.0", "stable", managed_by=update_stepup.LANE_ELECTRON)
        try:
            read = update_stepup.read_pending()
            assert read is not None
            assert read.managed_by == update_stepup.LANE_ELECTRON
            assert update_stepup.public_view(read)["managed_by"] == update_stepup.LANE_ELECTRON
        finally:
            update_stepup.clear_pending()

    def test_a_record_without_a_lane_reads_as_the_wheel_lane(self) -> None:
        """A record an older gateway wrote could only have been the wheel lane."""
        pending = update_stepup.arm("0.6.0", "stable")
        try:
            path = update_stepup.pending_path()
            raw = json.loads(path.read_text(encoding="utf-8"))
            del raw["managed_by"]
            path.write_text(json.dumps(raw), encoding="utf-8")
            read = update_stepup.read_pending()
            assert read is not None
            assert read.nonce == pending.nonce
            assert read.managed_by == update_stepup.LANE_WHEEL
        finally:
            update_stepup.clear_pending()

    def test_an_unknown_lane_reads_as_no_armed_request(self) -> None:
        update_stepup.arm("0.6.0", "stable")
        try:
            path = update_stepup.pending_path()
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["managed_by"] = "something-else"
            path.write_text(json.dumps(raw), encoding="utf-8")
            assert update_stepup.read_pending() is None
        finally:
            update_stepup.clear_pending()

    def test_arming_an_unknown_lane_is_refused(self) -> None:
        with pytest.raises(update_stepup.StepUpError):
            update_stepup.arm("0.6.0", "stable", managed_by="nope")
        assert update_stepup.read_pending() is None


@pytest.mark.asyncio
class TestArmPackagedApp:
    async def test_arms_the_version_the_desktop_host_reported(self, packaged) -> None:
        resp = await updates.api_update_arm(_request())
        assert resp.status == 200, resp.body
        payload = json.loads(resp.body.decode())
        assert payload["version"] == "0.6.0"
        assert payload["managed_by"] == update_stepup.LANE_ELECTRON
        pending = update_stepup.read_pending()
        assert pending is not None
        assert pending.managed_by == update_stepup.LANE_ELECTRON
        # The nonce is the authority and must never reach the caller.
        assert pending.nonce not in json.dumps(payload)

    async def test_refuses_when_no_desktop_host_is_polling(self, packaged) -> None:
        packaged.clear()
        resp = await updates.api_update_arm(_request())
        assert resp.status == 409
        assert json.loads(resp.body.decode())["code"] == "arm_no_app_host"
        assert update_stepup.read_pending() is None

    async def test_refuses_when_the_host_found_no_update(self, packaged) -> None:
        packaged.report_state(version="0.5.0", available_version="", channel="stable")
        resp = await updates.api_update_arm(_request())
        assert resp.status == 409
        assert json.loads(resp.body.decode())["code"] == "arm_no_verdict"

    async def test_refuses_below_the_minimum_version_floor(
        self, packaged, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(updates, "min_version", lambda: "0.9.0")
        monkeypatch.setattr(updates, "_local_version", "1.0.0")
        resp = await updates.api_update_arm(_request())
        assert resp.status == 409
        body = json.loads(resp.body.decode())
        assert body["code"] == "arm_below_min_version"
        assert body["governance"] is True

    async def test_policy_managed_host_refuses_before_consulting_the_bridge(
        self, packaged, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(updates, "resolve_provider", lambda: MagicMock())
        resp = await updates.api_update_arm(_request())
        assert resp.status == 409
        assert json.loads(resp.body.decode())["code"] == "arm_policy_managed"

    async def test_the_cached_cli_feed_verdict_is_not_consulted(self, packaged) -> None:
        """The packaged lane names the HOST's version, never the CLI feed's.

        The gateway's own check defers on a packaged install, so `_update_info`
        holds no candidate at all. An arm that read it would refuse forever.
        """
        updates._set_update_info()
        try:
            resp = await updates.api_update_arm(_request())
            assert resp.status == 200
            assert json.loads(resp.body.decode())["version"] == "0.6.0"
        finally:
            updates._set_update_info()


@pytest.mark.asyncio
class TestApprovePackagedApp:
    async def test_approval_queues_the_install_for_the_desktop_host(self, packaged) -> None:
        pending = update_stepup.arm("0.6.0", "stable", managed_by=update_stepup.LANE_ELECTRON)
        req = _request({"nonce": pending.nonce}, marks={"internal_auth": True})
        resp = await updates.api_update_approve(req)
        assert resp.status == 200, resp.body
        payload = json.loads(resp.body.decode())
        assert payload["status"] == "installing"
        assert payload["version"] == "0.6.0"
        queued = packaged.take_install()
        assert queued is not None
        assert queued.version == "0.6.0"
        assert queued.request_id == pending.request_id
        # Single-use: the arm is consumed, so a replayed approval cannot queue a
        # second install.
        assert update_stepup.read_pending() is None
        # Nothing was applied in-process: this gateway does not own the bytes.
        assert not req.app["state"]._background_tasks

    async def test_approval_without_an_arm_is_refused(self, packaged) -> None:
        update_stepup.clear_pending()
        resp = await updates.api_update_approve(
            _request({"nonce": "no-such-nonce"}, marks={"internal_auth": True})
        )
        assert resp.status == 403
        assert json.loads(resp.body.decode())["code"] == "approve_refused"
        assert packaged.take_install() is None

    async def test_a_wrong_nonce_does_not_queue_an_install(self, packaged) -> None:
        update_stepup.arm("0.6.0", "stable", managed_by=update_stepup.LANE_ELECTRON)
        resp = await updates.api_update_approve(
            _request({"nonce": "0" * 64}, marks={"internal_auth": True})
        )
        assert resp.status == 403
        assert packaged.take_install() is None
        # The armed request survives a failed approval attempt.
        assert update_stepup.read_pending() is not None

    async def test_an_expired_arm_cannot_be_approved(
        self, packaged, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending = update_stepup.arm("0.6.0", "stable", managed_by=update_stepup.LANE_ELECTRON)
        monkeypatch.setattr(
            update_stepup.time,
            "time",
            lambda: pending.created_at + update_stepup.PENDING_TTL_SECS + 1,
        )
        resp = await updates.api_update_approve(
            _request({"nonce": pending.nonce}, marks={"internal_auth": True})
        )
        assert resp.status == 403
        assert packaged.take_install() is None

    async def test_a_remote_caller_cannot_approve(self, packaged) -> None:
        pending = update_stepup.arm("0.6.0", "stable", managed_by=update_stepup.LANE_ELECTRON)
        resp = await updates.api_update_approve(
            _request({"nonce": pending.nonce}, remote="10.1.2.3")
        )
        assert resp.status == 403
        assert json.loads(resp.body.decode())["code"] == "approve_not_local"
        assert packaged.take_install() is None
        assert update_stepup.read_pending() is not None

    async def test_a_shape_change_refuses_rather_than_routing_the_wrong_apply(
        self, packaged, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An arm recorded for the app lane must not drive the wheel apply.

        The install's shape is re-derived at approve time and wins. Refusing is
        the only safe answer: the alternative routes an approval into a path
        that does not own this install's bytes.
        """
        from kiro_crew.platform import update_capability

        pending = update_stepup.arm("0.6.0", "stable", managed_by=update_stepup.LANE_ELECTRON)
        monkeypatch.setattr(update_capability, "derive_capability", _wheel_capability)
        resp = await updates.api_update_approve(
            _request({"nonce": pending.nonce}, marks={"internal_auth": True})
        )
        assert resp.status == 409
        assert json.loads(resp.body.decode())["code"] == "approve_shape_changed"
        assert packaged.take_install() is None

    async def test_a_wheel_arm_is_refused_on_a_packaged_install(self, packaged) -> None:
        """The mirror of the case above, and the one that protects the bytes."""
        pending = update_stepup.arm("0.6.0", "stable")
        resp = await updates.api_update_approve(
            _request({"nonce": pending.nonce}, marks={"internal_auth": True})
        )
        assert resp.status == 409
        assert json.loads(resp.body.decode())["code"] == "approve_shape_changed"
        assert packaged.take_install() is None


@pytest.mark.asyncio
class TestAppBridgeEndpoint:
    async def test_cookie_caller_is_refused(self, packaged) -> None:
        resp = await updates.api_update_app_bridge(_request({"state": {}}))
        assert resp.status == 403
        assert json.loads(resp.body.decode())["code"] == "loopback_only"

    async def test_reports_state_and_drains_one_install(self, packaged) -> None:
        packaged.clear()
        packaged.request_install(version="0.6.0", channel="stable", request_id="rid")
        resp = await updates.api_update_app_bridge(
            _request(
                {"state": {"version": "0.5.0", "available_version": "0.6.0", "channel": "stable"}},
                marks={"internal_auth": True},
            )
        )
        assert resp.status == 200
        payload = json.loads(resp.body.decode())
        assert payload["install"] == {
            "request_id": "rid",
            "version": "0.6.0",
            "channel": "stable",
        }
        state = packaged.host_state()
        assert state is not None and state.available_version == "0.6.0"
        # Drained: the next poll gets nothing.
        second = await updates.api_update_app_bridge(
            _request({"state": {}}, marks={"internal_auth": True})
        )
        assert json.loads(second.body.decode())["install"] is None

    async def test_non_string_and_oversized_fields_are_neutralised(self, packaged) -> None:
        resp = await updates.api_update_app_bridge(
            _request(
                {
                    "state": {
                        "version": {"not": "a string"},
                        "available_version": "v" * 500,
                        "channel": 7,
                    }
                },
                marks={"internal_auth": True},
            )
        )
        assert resp.status == 200
        state = packaged.host_state()
        assert state is not None
        assert state.version == ""
        assert state.channel == ""
        assert len(state.available_version) == 128

    async def test_a_missing_state_object_is_rejected(self, packaged) -> None:
        resp = await updates.api_update_app_bridge(
            _request({"nope": 1}, marks={"internal_auth": True})
        )
        assert resp.status == 400
        assert json.loads(resp.body.decode())["code"] == "state_invalid"

    async def test_invalid_json_is_rejected(self, packaged) -> None:
        resp = await updates.api_update_app_bridge(
            _request(ValueError("bad"), marks={"internal_auth": True})
        )
        assert resp.status == 400
        assert json.loads(resp.body.decode())["code"] == "invalid_json"
