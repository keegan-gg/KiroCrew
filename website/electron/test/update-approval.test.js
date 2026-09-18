// The desktop app's approval of an agent-armed update.
//
// The boundary this pins is WHERE the authority comes from and where it stops:
// reading the trust-fenced nonce from the data home is the host-identity proof,
// the nonce never leaves this process, and a window attached to a REMOTE gateway
// refuses outright rather than presenting one gateway's nonce to another.

const { test } = require("node:test");
const assert = require("node:assert");
const nodePath = require("path");
const {
  ARM_PATH,
  APPROVE_PATH,
  PENDING_FILENAME,
  TRUST_DIR,
  createUpdateApproval,
} = require("../update-approval");

const BASE = "http://127.0.0.1:6777";
const HOME = "/tmp/kc-home";
const NONCE = "a".repeat(64);

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

/** A fake fs whose readFileSync answers from `files`, else throws ENOENT. */
function fakeFs(files) {
  return {
    readFileSync(p) {
      if (Object.prototype.hasOwnProperty.call(files, p)) {
        const value = files[p];
        if (value instanceof Error) throw value;
        return value;
      }
      const err = new Error(`ENOENT: ${p}`);
      err.code = "ENOENT";
      throw err;
    },
  };
}

function deps(overrides) {
  const calls = [];
  const base = {
    fetchFn: async (url, init) => {
      calls.push({ url, init });
      return res(200, { ok: true, version: "0.6.0" });
    },
    getGatewayUrl: () => BASE,
    getSecret: () => "s3cr3t",
    isGatewayLocal: () => true,
    resolveHome: () => HOME,
    path: nodePath,
    fs: fakeFs({
      [nodePath.join(HOME, TRUST_DIR, PENDING_FILENAME)]: JSON.stringify({
        request_id: "r1",
        nonce: NONCE,
        version: "0.6.0",
        channel: "stable",
        managed_by: "electron",
      }),
    }),
    log: () => {},
  };
  return { deps: { ...base, ...overrides }, calls };
}

test("factory: missing required deps throw", () => {
  assert.throws(() => createUpdateApproval({}), /fetchFn is required/);
  assert.throws(
    () => createUpdateApproval({ fetchFn: () => {} }),
    /getGatewayUrl is required/,
  );
});

test("pendingPath mirrors the gateway's trust-fenced location", () => {
  const { deps: d } = deps();
  const approval = createUpdateApproval(d);
  assert.strictEqual(
    approval.pendingPath(),
    nodePath.join(HOME, TRUST_DIR, PENDING_FILENAME),
  );
});

test("armedStatus reads the gateway's projection, not the file", async () => {
  // The gateway owns the TTL and its expiry cleanup. Parsing the file here would
  // let the UI offer an "armed" request the gateway already dropped.
  const { deps: d, calls } = deps({
    fetchFn: async (url, init) => {
      calls_push(calls, url, init);
      return res(200, { armed: true, version: "0.6.0", managed_by: "electron", expires_in: 480 });
    },
  });
  const approval = createUpdateApproval(d);
  const status = await approval.armedStatus();
  assert.strictEqual(status.armed, true);
  assert.strictEqual(status.version, "0.6.0");
  assert.ok(calls[0].url.endsWith(ARM_PATH));
  assert.strictEqual(calls[0].init.method, "GET");
});

function calls_push(calls, url, init) {
  calls.push({ url, init });
}

test("armedStatus on a remote gateway answers unarmed without any request", async () => {
  const { deps: d, calls } = deps({ isGatewayLocal: () => false });
  const approval = createUpdateApproval(d);
  const status = await approval.armedStatus();
  assert.deepStrictEqual(status, { armed: false, reason: "remote-gateway" });
  assert.strictEqual(calls.length, 0);
});

test("armedStatus degrades to unarmed on an unreachable or malformed answer", async () => {
  const unreachable = createUpdateApproval(
    deps({ fetchFn: async () => { throw new Error("ECONNREFUSED"); } }).deps,
  );
  assert.deepStrictEqual(await unreachable.armedStatus(), {
    armed: false,
    reason: "unreachable",
  });
  const malformed = createUpdateApproval(
    deps({ fetchFn: async () => res(200, "THROW") }).deps,
  );
  assert.deepStrictEqual(await malformed.armedStatus(), {
    armed: false,
    reason: "malformed",
  });
  const refused = createUpdateApproval(deps({ fetchFn: async () => res(403, {}) }).deps);
  assert.deepStrictEqual(await refused.armedStatus(), { armed: false, reason: "http-403" });
});

test("approveArmed posts the nonce from the data home and returns only ok+version", async () => {
  const { deps: d, calls } = deps();
  const approval = createUpdateApproval(d);
  const out = await approval.approveArmed();
  assert.deepStrictEqual(out, { ok: true, version: "0.6.0" });
  assert.ok(calls[0].url.endsWith(APPROVE_PATH));
  assert.strictEqual(calls[0].init.method, "POST");
  assert.deepStrictEqual(JSON.parse(calls[0].init.body), { nonce: NONCE });
  assert.strictEqual(calls[0].init.headers["X-Internal-Secret"], "s3cr3t");
  // The nonce is single-use, but a copy in the renderer's return value would be
  // a copy of the one credential this mechanism keeps on disk.
  assert.ok(!JSON.stringify(out).includes(NONCE));
});

test("approveArmed refuses on a remote gateway without reading the nonce", async () => {
  // Approving across that seam presents one gateway's nonce to another.
  let read = false;
  const { deps: d, calls } = deps({
    isGatewayLocal: () => false,
    fs: {
      readFileSync() {
        read = true;
        return "{}";
      },
    },
  });
  const approval = createUpdateApproval(d);
  const out = await approval.approveArmed();
  assert.strictEqual(out.ok, false);
  assert.match(out.error, /remote gateway/);
  assert.strictEqual(read, false, "the nonce must not even be read");
  assert.strictEqual(calls.length, 0);
});

test("approveArmed reports a missing record as 'no armed request'", async () => {
  const { deps: d, calls } = deps({ fs: fakeFs({}) });
  const approval = createUpdateApproval(d);
  const out = await approval.approveArmed();
  assert.deepStrictEqual(out, { ok: false, error: "no armed update request" });
  assert.strictEqual(calls.length, 0);
});

test("approveArmed distinguishes an unreadable record from an absent one", async () => {
  const denied = new Error("EACCES");
  denied.code = "EACCES";
  const { deps: d } = deps({
    fs: fakeFs({ [nodePath.join(HOME, TRUST_DIR, PENDING_FILENAME)]: denied }),
  });
  const approval = createUpdateApproval(d);
  const out = await approval.approveArmed();
  assert.deepStrictEqual(out, {
    ok: false,
    error: "could not read the approval record",
  });
});

test("approveArmed refuses a malformed or nonce-less record", async () => {
  const p = nodePath.join(HOME, TRUST_DIR, PENDING_FILENAME);
  const bad = createUpdateApproval(deps({ fs: fakeFs({ [p]: "{not json" }) }).deps);
  assert.match((await bad.approveArmed()).error, /malformed/);
  const noNonce = createUpdateApproval(
    deps({ fs: fakeFs({ [p]: JSON.stringify({ version: "0.6.0" }) }) }).deps,
  );
  assert.match((await noNonce.approveArmed()).error, /malformed/);
});

test("approveArmed surfaces the gateway's own refusal text", async () => {
  const { deps: d } = deps({
    fetchFn: async () => res(409, {
      error: "the armed request targets the electron update lane",
      code: "approve_shape_changed",
    }),
  });
  const approval = createUpdateApproval(d);
  const out = await approval.approveArmed();
  assert.strictEqual(out.ok, false);
  assert.match(out.error, /electron update lane/);
});

test("approveArmed reports an unreachable gateway rather than throwing", async () => {
  const { deps: d } = deps({ fetchFn: async () => { throw new Error("ECONNREFUSED"); } });
  const approval = createUpdateApproval(d);
  const out = await approval.approveArmed();
  assert.deepStrictEqual(out, { ok: false, error: "the gateway is not reachable" });
});
