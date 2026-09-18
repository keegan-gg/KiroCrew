"""Gateway <-> packaged-desktop-host bridge for the app update lane.

An agent's update request originates in the Python gateway. On a packaged
install (``dmg``/``appimage``/``deb``/``rpm``) the bytes are replaced by the
Electron main process's updater, which lives in a DIFFERENT process that owns
``autoUpdater`` and the graceful ``stopGateway()`` handoff. So a request raised
on the Python side has to cross a process boundary to reach the only thing that
can act on it.

The direction of control is the same one ``browser/command_bus.py`` already
establishes, and for the same reason: rather than the gateway reaching INTO
Electron (it holds no handle on that process, and opening an inbound control
port on the process that owns the dashboard session is the exposure the browser
channel's design spike already ruled out), the gateway exposes a loopback queue
and **Electron pulls from it**:

* Electron polls ``POST /api/update/app-bridge`` (internal-secret only),
  reporting its updater's discovered state and draining at most one install
  request.
* The gateway records that state — it is the only source of the packaged lane's
  available version, since the CLI release feed describes a different release
  stream — and hands back an install request when a human has approved one.

What this module deliberately does NOT hold is authority. An entry here is a
*request*, produced only by ``POST /api/update/approve`` after the step-up
nonce was consumed (see :mod:`kiro_crew.platform.update_stepup`). Arming is an
agent-reachable action; approving is not. So the queue is only ever written on
the far side of a human approval, and a packaged host that drains it is acting
on a decision the gateway already verified.

State is in-memory and bounded by construction: exactly one host state record
and at most one outstanding install request. A gateway restart discards both,
which is the safe direction — a forgotten request must not survive to install
later, and the host re-reports its state on its next poll.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

#: How long a reported host state stays current. A packaged host polls every
#: ``APP_BRIDGE_POLL_MS`` (see ``website/electron/app-update-bridge.js``); this
#: is comfortably above that so one dropped poll does not read as "no desktop
#: host is present", while a host that has actually gone away stops answering
#: for the arm endpoint within a bounded window.
HOST_TTL_S = 120.0

#: How long an approved-but-undrained install request stays deliverable. The
#: step-up TTL already bounds how long an ARM is approvable; this bounds the
#: much shorter window between an approval landing and the host's next poll. A
#: request that ages out is dropped rather than delivered late, because "the
#: human approved this two hours ago" is not consent to quit the app now.
INSTALL_TTL_S = 300.0


@dataclass(frozen=True)
class AppHostState:
    """What a packaged desktop host last reported about its own updater."""

    #: The running app version, as the shell stamps it.
    version: str
    #: The version its updater found on the feed, or ``""`` when it found none.
    available_version: str
    #: The release lane that updater is following.
    channel: str
    #: Monotonic timestamp of the report.
    reported_at: float


@dataclass(frozen=True)
class InstallRequest:
    """One approved install, waiting for the host's next poll."""

    version: str
    channel: str
    request_id: str
    created_at: float


class AppUpdateBridge:
    """Single host state + at most one pending install request.

    Guarded by a plain :class:`threading.Lock` rather than an asyncio lock: the
    handlers that touch it are short and synchronous, and the same state is read
    from executor threads (the arm endpoint's off-loop capability probe runs
    beside it). ``now`` is injectable so the TTLs are testable without sleeping.
    """

    def __init__(self, *, now: Callable[[], float] | None = None) -> None:
        self._now = now or time.monotonic
        self._lock = threading.Lock()
        self._state: AppHostState | None = None
        self._pending: InstallRequest | None = None

    # ── host state ───────────────────────────────────────────────────────────

    def report_state(self, *, version: str, available_version: str, channel: str) -> AppHostState:
        """Record what the packaged host's updater currently knows."""
        state = AppHostState(
            version=str(version or ""),
            available_version=str(available_version or ""),
            channel=str(channel or ""),
            reported_at=self._now(),
        )
        with self._lock:
            self._state = state
        return state

    def host_state(self) -> AppHostState | None:
        """The current report, or ``None`` when absent or stale.

        Staleness is not an error to surface: a host that stopped polling is
        indistinguishable from one that was never there, and both mean the same
        thing to a caller — there is nothing on the far side to act on a
        request, so do not accept one.
        """
        with self._lock:
            state = self._state
            if state is None:
                return None
            if self._now() - state.reported_at > HOST_TTL_S:
                return None
            return state

    def host_present(self) -> bool:
        return self.host_state() is not None

    # ── install requests ─────────────────────────────────────────────────────

    def request_install(self, *, version: str, channel: str, request_id: str) -> InstallRequest:
        """Queue one approved install. Last writer wins.

        Overwriting is correct rather than merely tolerable: each request is the
        product of its own consumed single-use nonce, so a second approval is a
        second human decision, and the newer one describes what the human most
        recently agreed to. Nothing is lost that an install could have honored —
        only one bundle swap can happen.
        """
        req = InstallRequest(
            version=str(version or ""),
            channel=str(channel or ""),
            request_id=str(request_id or ""),
            created_at=self._now(),
        )
        with self._lock:
            self._pending = req
        logger.info(
            "Queued packaged-app install request %s (v%s, %s channel)",
            req.request_id,
            req.version,
            req.channel,
        )
        return req

    def take_install(self) -> InstallRequest | None:
        """Pop the pending request, or ``None``. Single delivery.

        Popping BEFORE the host acts is deliberate: an install quits the app, so
        a request that is delivered and then lost to a crash must not be
        redelivered on the next launch, where the human's approval is long past
        and the quit would be unexplained. Re-approving is cheap; a surprise
        restart is not.
        """
        with self._lock:
            req = self._pending
            self._pending = None
        if req is None:
            return None
        if self._now() - req.created_at > INSTALL_TTL_S:
            logger.info(
                "Dropping packaged-app install request %s: approved %.0fs ago, past the "
                "%.0fs delivery window",
                req.request_id,
                self._now() - req.created_at,
                INSTALL_TTL_S,
            )
            return None
        return req

    def pending_install(self) -> InstallRequest | None:
        """Peek without consuming. For status surfaces and tests only."""
        with self._lock:
            req = self._pending
        if req is None or self._now() - req.created_at > INSTALL_TTL_S:
            return None
        return req

    def clear(self) -> None:
        """Drop all state. Used by tests and by an explicit arm cancellation."""
        with self._lock:
            self._state = None
            self._pending = None


_bridge: AppUpdateBridge | None = None
_bridge_lock = threading.Lock()


def get_app_update_bridge() -> AppUpdateBridge:
    """The process-wide bridge."""
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            _bridge = AppUpdateBridge()
        return _bridge


__all__ = [
    "HOST_TTL_S",
    "INSTALL_TTL_S",
    "AppHostState",
    "AppUpdateBridge",
    "InstallRequest",
    "get_app_update_bridge",
]
