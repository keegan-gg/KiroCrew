// Agent -> Electron app-update channel (the CLIENT side).
//
// An agent asking for the desktop app to update raises that request in the
// Python gateway, where the agent surface lives. But the bytes belong to THIS
// process: `autoUpdater`, the staged bundle and the awaited `stopGateway()`
// handoff only exist here. So the request has to cross the process boundary to
// reach the only thing that can act on it.
//
// The direction of control is the same one browser-agent-channel.js already
// established, and for the same reason — the gateway holds no handle on this
// process, and opening an inbound control port on the process that owns the
// dashboard session is exactly the exposure that design ruled out. So the
// gateway exposes a loopback queue and Electron PULLS from it:
//
//   • Electron polls  POST /api/update/app-bridge  with its updater's current
//     state (running version, the version its feed offers, the lane it follows).
//   • That state is the gateway's ONLY source of the packaged lane's available
//     version — the CLI release feed the gateway reads describes a different
//     release stream — so reporting it is what lets `POST /api/update/arm` name
//     a real target instead of refusing every packaged install.
//   • The same answer carries at most one APPROVED install request, which this
//     loop hands to the updater.
//
// ── What arriving here does and does not authorize ───────────────────────────
//
// An install request is only ever produced on the far side of a consumed
// step-up nonce: an agent can ARM an update, and only a caller that read the
// gateway host's trust-fenced nonce file can approve one. So this loop does not
// decide anything — it delivers a decision that was already made and audited,
// and it drives the SAME path the human's Settings > About button drives. It
// does not enable install-on-quit (`autoInstallOnAppQuit` stays false; see
// auto-update.js) and it never calls `quitAndInstall` directly.
//
// ── Why the loop must be defensive ──────────────────────────────────────────
//
// It runs for the life of the app, so the three failure modes the browser
// channel documents apply unchanged: a throwing install dispatch must not kill
// it, a transport error or non-2xx must back off rather than tight-spin, and a
// malformed body must be reported and skipped. Beyond those, ONE more matters
// here: an install request must be dispatched at most once per delivery. The
// gateway pops it, so a loop that re-dispatched on its own would quit the app
// twice for one approval.
//
// ── Testability ─────────────────────────────────────────────────────────────
//
// Every dependency is injected, mirroring browser-agent-channel.js, so the loop
// is unit-testable with a fake `fetchFn`, no Electron, and no real timers:
//
//   fetchFn(url, { method, headers, body }) -> { ok, status, async json() }
//   getGatewayUrl()  -> loopback gateway base URL
//   getSecret()      -> X-Internal-Secret value (NEVER read a file here)
//   getUpdaterState()-> { version, available_version, channel }
//   installApproved({ version, channel, request_id }) -> Promise
//   isGatewayLocal() -> is the gateway on THIS machine?
//   onError(err, ctx)-> log sink; must never throw meaningfully
"use strict";

const BRIDGE_PATH = "/api/update/app-bridge";

// Poll interval (ms). The gateway holds no long-poll here: an approval is a
// human action whose latency budget is seconds, not milliseconds, and a short
// fixed poll keeps the reported updater state fresh without holding a request
// open for the life of the app. Must stay well below the gateway's HOST_TTL_S
// (app_update_bridge.py) so one dropped poll never reads as "no host present".
const DEFAULT_POLL_MS = 30000;
const DEFAULT_BACKOFF_MS = 5000;

/** Join a base URL and a path without doubling or dropping the slash. */
function joinUrl(base, path) {
  const b = String(base || "").replace(/\/+$/, "");
  return `${b}${path}`;
}

/**
 * Normalise an updater state reading into the three strings the gateway takes.
 *
 * A throwing or malformed reader must still produce a report: the host-presence
 * signal is what keeps the arm endpoint answering "the app is reachable", and
 * losing it because a version field came back undefined would silently take the
 * whole feature away.
 */
function normaliseState(raw) {
  const s = raw && typeof raw === "object" ? raw : {};
  const str = (v) => (typeof v === "string" ? v : "");
  return {
    version: str(s.version),
    available_version: str(s.available_version),
    channel: str(s.channel),
  };
}

/**
 * Validate a drained install request. Returns the request or null.
 *
 * Strict on purpose: this value quits the application. A payload missing its
 * request id or version is a bug somewhere, and acting on it would produce an
 * unexplained restart — the exact failure the step-up exists to make impossible.
 */
function validateInstall(raw) {
  if (!raw || typeof raw !== "object") return null;
  if (typeof raw.request_id !== "string" || !raw.request_id) return null;
  if (typeof raw.version !== "string" || !raw.version) return null;
  return {
    request_id: raw.request_id,
    version: raw.version,
    channel: typeof raw.channel === "string" ? raw.channel : "",
  };
}

function createAppUpdateBridge(deps) {
  const {
    fetchFn,
    getGatewayUrl,
    getSecret,
    getUpdaterState,
    installApproved,
    isGatewayLocal = () => false,
    onError = () => {},
    pollMs = DEFAULT_POLL_MS,
    backoffMs = DEFAULT_BACKOFF_MS,
  } = deps || {};

  if (typeof fetchFn !== "function") throw new Error("createAppUpdateBridge: fetchFn is required");
  if (typeof getGatewayUrl !== "function") throw new Error("createAppUpdateBridge: getGatewayUrl is required");
  if (typeof getSecret !== "function") throw new Error("createAppUpdateBridge: getSecret is required");
  if (typeof getUpdaterState !== "function") throw new Error("createAppUpdateBridge: getUpdaterState is required");
  if (typeof installApproved !== "function") throw new Error("createAppUpdateBridge: installApproved is required");

  let running = false;
  let wakeSleep = null;
  let loopDone = null;
  // Request ids already dispatched. The gateway pops a request on delivery, so
  // this is belt-and-braces against a retry that re-delivers one: an install
  // quits the app, and doing it twice for one approval is not recoverable by
  // the user.
  const dispatched = new Set();

  const report = (err, context) => {
    try {
      onError(err, context);
    } catch {
      /* a log sink must never break the loop */
    }
  };

  function sleep(ms) {
    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        wakeSleep = null;
        resolve();
      };
      wakeSleep = finish;
      const timer = setTimeout(finish, Math.max(0, ms));
      // A poll that runs for the life of the app must never be the reason the
      // process stays alive: unref'd, exactly like auto-update.js's own launch
      // and poll timers. Without this the loop keeps Node's event loop busy
      // forever, which hangs any harness that merely constructed the shell.
      if (timer && typeof timer.unref === "function") timer.unref();
    });
  }

  function headers() {
    const h = { "Content-Type": "application/json" };
    const secret = getSecret();
    if (secret) h["X-Internal-Secret"] = secret;
    return h;
  }

  /**
   * One poll. Returns the validated install request, `null` when there is
   * nothing to install, or `undefined` on a transport error / non-2xx /
   * malformed body — the distinction is what lets the caller back off without
   * inspecting HTTP itself (same contract as the browser channel's drainOnce).
   */
  async function pollOnce() {
    let state;
    try {
      state = normaliseState(await getUpdaterState());
    } catch (err) {
      // A throwing reader still reports presence: see normaliseState.
      report(err, { phase: "updater-state" });
      state = normaliseState(null);
    }
    let res;
    try {
      res = await fetchFn(joinUrl(getGatewayUrl(), BRIDGE_PATH), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ state }),
      });
    } catch (err) {
      report(err, { phase: "bridge-fetch" });
      return undefined;
    }
    if (!res || typeof res.status !== "number") {
      report(new Error("bridge: malformed response object"), { phase: "bridge-response" });
      return undefined;
    }
    if (!res.ok) {
      report(new Error(`bridge: HTTP ${res.status}`), { phase: "bridge-status", status: res.status });
      return undefined;
    }
    let body;
    try {
      body = await res.json();
    } catch (err) {
      report(err, { phase: "bridge-parse" });
      return undefined;
    }
    if (!body || typeof body !== "object") {
      report(new Error("bridge: malformed body"), { phase: "bridge-validate" });
      return undefined;
    }
    // An absent / null `install` is the ordinary answer, not a fault.
    if (body.install === null || body.install === undefined) return null;
    const install = validateInstall(body.install);
    if (install === null) {
      report(new Error("bridge: malformed install request"), { phase: "bridge-install-validate" });
      return undefined;
    }
    return install;
  }

  async function dispatchInstall(install) {
    if (dispatched.has(install.request_id)) {
      report(
        new Error(`bridge: install ${install.request_id} already dispatched`),
        { phase: "install-duplicate", id: install.request_id },
      );
      return;
    }
    dispatched.add(install.request_id);
    try {
      await installApproved(install);
    } catch (err) {
      report(err, { phase: "install-dispatch", id: install.request_id });
    }
  }

  async function loop() {
    while (running) {
      // A window attached to a REMOTE gateway must not push the local secret
      // through the tunnel, and a remote gateway's approvals are not about this
      // machine's bundle anyway. Same predicate the browser channel's heartbeat
      // uses, and the same reason.
      if (!isGatewayLocal()) {
        if (running) await sleep(pollMs);
        continue;
      }
      const install = await pollOnce();
      if (!running) break;
      if (install === undefined) {
        await sleep(backoffMs);
        continue;
      }
      if (install !== null) {
        // A failing dispatch must never kill the loop.
        try {
          await dispatchInstall(install);
        } catch (err) {
          report(err, { phase: "install", id: install.request_id });
        }
      }
      if (running) await sleep(pollMs);
    }
  }

  return {
    /** Start polling. Idempotent. */
    start() {
      if (running) return;
      running = true;
      loopDone = loop().catch((err) => {
        report(err, { phase: "loop-crash" });
        running = false;
      });
    },

    /** Poll NOW rather than waiting out the interval (e.g. after a check). */
    poke() {
      if (wakeSleep) wakeSleep();
    },

    /** Stop cleanly, interrupting an in-flight sleep. Resolves once unwound. */
    async stop() {
      running = false;
      if (wakeSleep) wakeSleep();
      const done = loopDone;
      loopDone = null;
      if (done) await done;
    },

    /** Test seam. */
    isRunning: () => running,
  };
}

module.exports = {
  BRIDGE_PATH,
  DEFAULT_POLL_MS,
  DEFAULT_BACKOFF_MS,
  joinUrl,
  normaliseState,
  validateInstall,
  createAppUpdateBridge,
};
