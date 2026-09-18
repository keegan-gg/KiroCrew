// The packaged app's half of the agent-armed update lane.
//
// What is pinned here is the loop's discipline, not its plumbing: it reports
// state so the gateway can name a target version, it dispatches an approved
// install AT MOST ONCE, it refuses to poll a remote gateway with this machine's
// local secret, and no single failure (a throwing state reader, a non-2xx, a
// malformed body, a rejecting install) can kill it or make it spin.

const { test } = require("node:test");
const assert = require("node:assert");
const {
  BRIDGE_PATH,
  joinUrl,
  normaliseState,
  validateInstall,
  createAppUpdateBridge,
} = require("../app-update-bridge");

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function waitFor(fn, timeout = 1000) {
  const start = Date.now();
  for (;;) {
    const v = fn();
    if (v) return v;
    if (Date.now() - start > timeout) throw new Error("waitFor: timed out");
    await sleep(2);
  }
}

function res(status, jsonBody) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      if (jsonBody === "THROW") throw new Error("bad json");
      return jsonBody;
    },
  };
}

const BASE = "http://127.0.0.1:6777";

/** A fake fetch that answers the bridge path from `responder(callIndex, body)`. */
function fakeFetch(responder) {
  const calls = { count: 0, bodies: [], headers: [] };
  async function fetchFn(url, opts) {
    if (!url.endsWith(BRIDGE_PATH)) throw new Error(`unexpected url ${url}`);
    const idx = calls.count;
    calls.count += 1;
    calls.bodies.push(opts && opts.body ? JSON.parse(opts.body) : {});
    calls.headers.push((opts && opts.headers) || {});
    return responder(idx, calls.bodies[idx]);
  }
  return { fetchFn, calls };
}

function baseDeps(overrides) {
  return {
    getGatewayUrl: () => BASE,
    getSecret: () => "s3cr3t",
    getUpdaterState: () => ({ version: "0.5.0", available_version: "0.6.0", channel: "stable" }),
    installApproved: async () => {},
    isGatewayLocal: () => true,
    onError: () => {},
    pollMs: 10,
    backoffMs: 10,
    ...overrides,
  };
}

// ── pure helpers ──

test("joinUrl: no double slash, path preserved", () => {
  assert.strictEqual(joinUrl(`${BASE}/`, BRIDGE_PATH), `${BASE}${BRIDGE_PATH}`);
  assert.strictEqual(joinUrl(BASE, BRIDGE_PATH), `${BASE}${BRIDGE_PATH}`);
});

test("normaliseState: non-strings and absent fields become empty strings", () => {
  assert.deepStrictEqual(normaliseState(null), {
    version: "",
    available_version: "",
    channel: "",
  });
  assert.deepStrictEqual(
    normaliseState({ version: 7, available_version: "0.6.0", channel: {} }),
    { version: "", available_version: "0.6.0", channel: "" },
  );
});

test("validateInstall: a payload that cannot name a version is refused", () => {
  // This value quits the application, so a malformed one must never be acted on.
  assert.strictEqual(validateInstall(null), null);
  assert.strictEqual(validateInstall({ version: "0.6.0" }), null, "no request_id");
  assert.strictEqual(validateInstall({ request_id: "r" }), null, "no version");
  assert.deepStrictEqual(validateInstall({ request_id: "r", version: "0.6.0" }), {
    request_id: "r",
    version: "0.6.0",
    channel: "",
  });
});

test("factory: missing required deps throw", () => {
  assert.throws(() => createAppUpdateBridge({}), /fetchFn is required/);
  assert.throws(() => createAppUpdateBridge({ fetchFn: () => {} }), /getGatewayUrl is required/);
  assert.throws(
    () => createAppUpdateBridge({ fetchFn: () => {}, getGatewayUrl: () => BASE, getSecret: () => "" }),
    /getUpdaterState is required/,
  );
});

// ── reporting ──

test("reports the updater state and carries the internal secret", async () => {
  const { fetchFn, calls } = fakeFetch(() => res(200, { ok: true, install: null }));
  const bridge = createAppUpdateBridge(baseDeps({ fetchFn }));
  bridge.start();
  await waitFor(() => calls.count >= 1);
  await bridge.stop();
  assert.deepStrictEqual(calls.bodies[0].state, {
    version: "0.5.0",
    available_version: "0.6.0",
    channel: "stable",
  });
  assert.strictEqual(calls.headers[0]["X-Internal-Secret"], "s3cr3t");
});

test("a throwing state reader still reports, so host presence survives", async () => {
  const { fetchFn, calls } = fakeFetch(() => res(200, { ok: true, install: null }));
  const errors = [];
  const bridge = createAppUpdateBridge(
    baseDeps({
      fetchFn,
      getUpdaterState: () => { throw new Error("updater not ready"); },
      onError: (err, ctx) => errors.push(ctx.phase),
    }),
  );
  bridge.start();
  await waitFor(() => calls.count >= 1);
  await bridge.stop();
  // Losing the poll would take the whole feature away: the arm endpoint would
  // read "no desktop host is present" and refuse forever.
  assert.deepStrictEqual(calls.bodies[0].state, {
    version: "",
    available_version: "",
    channel: "",
  });
  assert.ok(errors.includes("updater-state"));
});

// ── remote gateway ──

test("a remote gateway is never polled", async () => {
  const { fetchFn, calls } = fakeFetch(() => res(200, { ok: true, install: null }));
  const bridge = createAppUpdateBridge(baseDeps({ fetchFn, isGatewayLocal: () => false }));
  bridge.start();
  await sleep(50); // several poll intervals at pollMs=10
  await bridge.stop();
  // The local secret must not cross a tunnel, and a remote gateway's approvals
  // are not about this machine's bundle.
  assert.strictEqual(calls.count, 0);
  assert.strictEqual(bridge.isRunning(), false);
});

// ── install dispatch ──

test("an approved install is dispatched with its version", async () => {
  const installed = [];
  const { fetchFn } = fakeFetch((idx) =>
    idx === 0
      ? res(200, { ok: true, install: { request_id: "r1", version: "0.6.0", channel: "stable" } })
      : res(200, { ok: true, install: null }),
  );
  const bridge = createAppUpdateBridge(
    baseDeps({ fetchFn, installApproved: async (r) => { installed.push(r); } }),
  );
  bridge.start();
  await waitFor(() => installed.length >= 1);
  await bridge.stop();
  assert.deepStrictEqual(installed[0], {
    request_id: "r1",
    version: "0.6.0",
    channel: "stable",
  });
});

test("the same request id is dispatched at most once", async () => {
  // The gateway pops a request on delivery, so a redelivery is a bug somewhere.
  // Acting on it twice quits the app twice for one approval, which the user
  // cannot undo -- so the loop refuses rather than trusting the server.
  const installed = [];
  const { fetchFn } = fakeFetch(() =>
    res(200, { ok: true, install: { request_id: "same", version: "0.6.0", channel: "stable" } }),
  );
  const errors = [];
  const bridge = createAppUpdateBridge(
    baseDeps({
      fetchFn,
      installApproved: async (r) => { installed.push(r); },
      onError: (err, ctx) => errors.push(ctx.phase),
    }),
  );
  bridge.start();
  await waitFor(() => errors.includes("install-duplicate"));
  await bridge.stop();
  assert.strictEqual(installed.length, 1);
});

test("a rejecting install does not kill the loop", async () => {
  const { fetchFn, calls } = fakeFetch((idx) =>
    idx === 0
      ? res(200, { ok: true, install: { request_id: "r1", version: "0.6.0", channel: "stable" } })
      : res(200, { ok: true, install: null }),
  );
  const errors = [];
  const bridge = createAppUpdateBridge(
    baseDeps({
      fetchFn,
      installApproved: async () => { throw new Error("updater exploded"); },
      onError: (err, ctx) => errors.push(ctx.phase),
    }),
  );
  bridge.start();
  await waitFor(() => calls.count >= 3);
  assert.strictEqual(bridge.isRunning(), true);
  await bridge.stop();
  assert.ok(errors.includes("install-dispatch"));
});

// ── failure handling ──

test("a non-2xx backs off and keeps polling", async () => {
  const { fetchFn, calls } = fakeFetch((idx) =>
    idx < 2 ? res(503, {}) : res(200, { ok: true, install: null }),
  );
  const errors = [];
  const bridge = createAppUpdateBridge(
    baseDeps({ fetchFn, onError: (err, ctx) => errors.push(ctx.phase) }),
  );
  bridge.start();
  await waitFor(() => calls.count >= 3);
  await bridge.stop();
  assert.ok(errors.includes("bridge-status"));
});

test("a transport error, a malformed body and a bad install payload all recover", async () => {
  const { fetchFn, calls } = fakeFetch((idx) => {
    if (idx === 0) throw new Error("ECONNREFUSED");
    if (idx === 1) return res(200, "THROW");
    if (idx === 2) return res(200, null);
    if (idx === 3) return res(200, { ok: true, install: { version: "0.6.0" } });
    return res(200, { ok: true, install: null });
  });
  const errors = [];
  const installed = [];
  const bridge = createAppUpdateBridge(
    baseDeps({
      fetchFn,
      installApproved: async (r) => { installed.push(r); },
      onError: (err, ctx) => errors.push(ctx.phase),
    }),
  );
  bridge.start();
  await waitFor(() => calls.count >= 5);
  await bridge.stop();
  for (const phase of ["bridge-fetch", "bridge-parse", "bridge-validate", "bridge-install-validate"]) {
    assert.ok(errors.includes(phase), `expected a ${phase} report, got ${errors.join(",")}`);
  }
  assert.strictEqual(installed.length, 0, "a payload with no request id must not install");
});

test("stop() unwinds the loop and is safe twice", async () => {
  const { fetchFn } = fakeFetch(() => res(200, { ok: true, install: null }));
  const bridge = createAppUpdateBridge(baseDeps({ fetchFn, pollMs: 5000 }));
  bridge.start();
  bridge.start(); // idempotent
  await sleep(20);
  await bridge.stop();
  await bridge.stop();
  assert.strictEqual(bridge.isRunning(), false);
});
