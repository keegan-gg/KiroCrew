// The desktop app's half of the update step-up: reading the approval nonce and
// presenting it back to the gateway.
//
// ── Why this process may approve at all ──────────────────────────────────────
//
// The gateway splits an in-app update into two actions with different
// authority (src/kiro_crew/platform/update_stepup.py): ARMING records the
// request and writes a single-use nonce into the data home, and APPROVING means
// presenting that nonce back. Reading the file requires filesystem access as the
// gateway's own user, which is precisely the identity the split exists to prove
// — and precisely what the adversary it defends against, a network-reachable
// dashboard bearer, cannot do.
//
// The Electron main process on the gateway's own host HAS that identity. So an
// approval driven from Settings > About in the desktop app is the same class of
// evidence as `kirocrew update approve` typed into a terminal on that host: not
// a weaker one, and not a new authority. What it adds is reach — a chat-only
// user who never opens a terminal can still approve what an agent armed, on
// their next visit to the app.
//
// ── The one case where it must refuse ───────────────────────────────────────
//
// A desktop window can be pointed at a REMOTE gateway. The armed request then
// lives on that remote host while the nonce file under this machine's data home
// belongs to a different (local) gateway entirely. Approving across that seam
// would present one gateway's nonce to another — at best a refusal, at worst
// approving a local install nobody asked about from a remote dashboard's button.
// So every entry point here is gated on the gateway being local, and the gate is
// the caller's to supply (`isGatewayLocal`) because only the shell knows which
// gateway this window attached to.
//
// Every dependency is injected so the whole module is testable with fakes and no
// Electron, no filesystem and no network.
"use strict";

const nodeFs = require("fs");
const nodePath = require("path");
const { resolveHome: defaultResolveHome } = require("./home-dir");

// Must match update_stepup._PENDING_FILENAME and its trust/ directory. A
// mismatch here does not fail loudly — it reads as "nothing is armed" forever —
// so the constant is named and tested rather than inlined at the call site.
const TRUST_DIR = "trust";
const PENDING_FILENAME = "pending-update-approval.json";

const ARM_PATH = "/api/update/arm";
const APPROVE_PATH = "/api/update/approve";

function joinUrl(base, path) {
  const b = String(base || "").replace(/\/+$/, "");
  return `${b}${path}`;
}

function createUpdateApproval(deps) {
  const {
    fetchFn,
    getGatewayUrl,
    getSecret,
    isGatewayLocal = () => false,
    fs = nodeFs,
    path = nodePath,
    resolveHome = defaultResolveHome,
    log = () => {},
  } = deps || {};

  if (typeof fetchFn !== "function") throw new Error("createUpdateApproval: fetchFn is required");
  if (typeof getGatewayUrl !== "function") throw new Error("createUpdateApproval: getGatewayUrl is required");
  if (typeof getSecret !== "function") throw new Error("createUpdateApproval: getSecret is required");

  function pendingPath() {
    return path.join(resolveHome(), TRUST_DIR, PENDING_FILENAME);
  }

  function headers() {
    const h = { "Content-Type": "application/json" };
    const secret = getSecret();
    if (secret) h["X-Internal-Secret"] = secret;
    return h;
  }

  /**
   * The armed request as the gateway projects it, or `{ armed: false }`.
   *
   * Read over HTTP rather than from the nonce file: the gateway owns the TTL and
   * the expiry cleanup, and a file this process parsed itself would let the UI
   * show an "armed" request the gateway has already dropped. The nonce is not in
   * that projection, which is the point — nothing that renders needs it.
   */
  async function armedStatus() {
    if (!isGatewayLocal()) return { armed: false, reason: "remote-gateway" };
    let res;
    try {
      res = await fetchFn(joinUrl(getGatewayUrl(), ARM_PATH), {
        method: "GET",
        headers: headers(),
      });
    } catch (err) {
      log(`[update] armed-status fetch failed: ${(err && err.message) || err}`);
      return { armed: false, reason: "unreachable" };
    }
    if (!res || !res.ok) return { armed: false, reason: `http-${res && res.status}` };
    try {
      const body = await res.json();
      return body && typeof body === "object" ? body : { armed: false };
    } catch (err) {
      log(`[update] armed-status parse failed: ${(err && err.message) || err}`);
      return { armed: false, reason: "malformed" };
    }
  }

  /**
   * Read the nonce and POST it to the gateway's approve endpoint.
   *
   * Returns `{ ok: true, version }` when the gateway accepted, or
   * `{ ok: false, error }` otherwise. The nonce NEVER appears in the return
   * value or in a log line: it is single-use, but a nonce in the renderer's
   * console or in the shell log is a copy of the one credential this whole
   * mechanism exists to keep on disk.
   */
  async function approveArmed() {
    if (!isGatewayLocal()) {
      return { ok: false, error: "this window is attached to a remote gateway" };
    }
    let raw;
    try {
      raw = fs.readFileSync(pendingPath(), "utf-8");
    } catch (err) {
      // ENOENT is the ordinary "nothing armed" case and is not worth a scary
      // error; anything else (a permission problem on the data home) is.
      const missing = err && err.code === "ENOENT";
      log(`[update] approve: nonce unreadable (${(err && err.code) || err})`);
      return {
        ok: false,
        error: missing ? "no armed update request" : "could not read the approval record",
      };
    }
    let nonce;
    try {
      const parsed = JSON.parse(raw);
      nonce = parsed && typeof parsed.nonce === "string" ? parsed.nonce : "";
    } catch {
      return { ok: false, error: "the approval record is malformed" };
    }
    if (!nonce) return { ok: false, error: "the approval record is malformed" };
    let res;
    try {
      res = await fetchFn(joinUrl(getGatewayUrl(), APPROVE_PATH), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ nonce }),
      });
    } catch (err) {
      log(`[update] approve POST failed: ${(err && err.message) || err}`);
      return { ok: false, error: "the gateway is not reachable" };
    }
    let body = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    if (!res || !res.ok) {
      const detail = body && typeof body.error === "string" ? body.error : "";
      log(`[update] approve refused (HTTP ${res && res.status})`);
      return {
        ok: false,
        error: detail || `the gateway refused the approval (HTTP ${res && res.status})`,
      };
    }
    log("[update] approval accepted by the gateway");
    return { ok: true, version: (body && body.version) || "" };
  }

  return { armedStatus, approveArmed, pendingPath };
}

module.exports = {
  ARM_PATH,
  APPROVE_PATH,
  PENDING_FILENAME,
  TRUST_DIR,
  joinUrl,
  createUpdateApproval,
};
