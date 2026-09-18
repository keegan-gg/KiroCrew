// The APPROVED install path (issue #503): an update an agent armed and a human
// approved installs without a second click.
//
// Two things make this safe to have at all, and each one is pinned here.
//
// 1. It reuses applyUpdateAndRestart() -- the awaited stopGateway() +
//    quitAndInstall sequence the human's Install button already uses -- so
//    autoInstallOnAppQuit stays false and no new install-on-quit path exists.
// 2. The arming flag is ONE-SHOT. Every outcome that can follow a check either
//    consumes it (update-downloaded) or releases it (up-to-date, a retraction,
//    an error). A flag that survived its outcome would install whatever a later,
//    unrelated download happened to stage -- which nobody approved.

const { test } = require("node:test");
const assert = require("node:assert");

const { initAutoUpdate } = require("../auto-update");

function makeDeps({ appVersion = "1.0.0", autoDownload = false } = {}) {
  const calls = {
    quitAndInstall: [],
    checks: 0,
    downloads: 0,
    states: [],
    stopped: 0,
  };
  const handlers = {};
  const autoUpdater = {
    setFeedURL: () => {},
    checkForUpdates: async () => { calls.checks += 1; },
    downloadUpdate: async () => { calls.downloads += 1; },
    quitAndInstall: (...a) => calls.quitAndInstall.push(a),
    on: (ev, fn) => { handlers[ev] = fn; },
  };
  const deps = {
    app: {
      isPackaged: true,
      getVersion: () => appVersion,
      once: () => {},
      removeListener: () => {},
      exit: () => {},
    },
    autoUpdater,
    dialog: { showMessageBox: async () => ({ response: 1 }) },
    Notification: function () { return { show: () => {} }; },
    getFlavor: () => "stable",
    getAutoDownloadPreference: () => autoDownload,
    stopGateway: async () => { calls.stopped += 1; },
    osPlatform: "darwin",
    feedBase: "https://cdn.example.dev/feed",
    onUpdateState: (payload) => calls.states.push(payload),
    log: { info: () => {}, warn: () => {}, error: () => {} },
    nativeAutoUpdater: { once: () => {} },
    uiDriven: true,
  };
  return { deps, calls, emit: (ev, p) => handlers[ev] && handlers[ev](p) };
}

/** Let the fire-and-forget install dispatch drain. */
function settle() {
  return new Promise((r) => setTimeout(r, 5));
}

test("the handle exposes the bridge's two entry points", () => {
  const { deps } = makeDeps();
  const u = initAutoUpdate(deps);
  assert.strictEqual(typeof u.installApproved, "function");
  assert.strictEqual(typeof u.bridgeState, "function");
});

test("bridgeState reports what the gateway has no other source for", () => {
  // The gateway's own check DEFERS on a packaged install, so without this the
  // arm endpoint has no target version and refuses every packaged install.
  const { deps, emit } = makeDeps();
  const u = initAutoUpdate(deps);
  assert.deepStrictEqual(u.bridgeState(), {
    version: "1.0.0",
    available_version: "",
    channel: "stable",
  });
  emit("update-available", { version: "1.1.0" });
  assert.strictEqual(u.bridgeState().available_version, "1.1.0");
});

test("a staged build counts as available, so an arm can name it", () => {
  const { deps, emit } = makeDeps();
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0" });
  assert.strictEqual(u.bridgeState().available_version, "1.1.0");
});

test("an approved install of an already-staged build installs at once", async () => {
  const { deps, calls, emit } = makeDeps();
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0" });
  await u.installApproved({ version: "1.1.0" });
  assert.strictEqual(calls.stopped, 1, "the gateway must be stopped before the swap");
  assert.strictEqual(calls.quitAndInstall.length, 1);
});

test("an approved install with nothing staged discovers, downloads, then installs", async () => {
  // autoDownload is OFF: the preference governs UNATTENDED downloads, and this
  // one is not unattended. Refusing to fetch would make the approval do nothing.
  const { deps, calls, emit } = makeDeps({ autoDownload: false });
  const u = initAutoUpdate(deps);
  await u.installApproved({ version: "1.1.0" });
  assert.strictEqual(calls.checks, 1, "an approval must consult the feed");
  emit("update-available", { version: "1.1.0" });
  await settle();
  assert.strictEqual(calls.downloads, 1, "an approved install downloads without a second click");
  emit("update-downloaded", { version: "1.1.0" });
  await settle();
  assert.strictEqual(calls.quitAndInstall.length, 1);
  assert.strictEqual(calls.stopped, 1);
});

test("the approval is consumed: a SECOND download does not install again", async () => {
  const { deps, calls, emit } = makeDeps();
  const u = initAutoUpdate(deps);
  await u.installApproved({ version: "1.1.0" });
  emit("update-available", { version: "1.1.0" });
  await settle();
  emit("update-downloaded", { version: "1.1.0" });
  await settle();
  assert.strictEqual(calls.quitAndInstall.length, 1);
  // A later, unapproved download must not ride the same approval.
  emit("update-downloaded", { version: "1.2.0" });
  await settle();
  assert.strictEqual(
    calls.quitAndInstall.length,
    1,
    "one approval must produce at most one install",
  );
});

test("an up-to-date answer releases the approval instead of leaving it armed", async () => {
  const { deps, calls, emit } = makeDeps();
  const u = initAutoUpdate(deps);
  await u.installApproved({ version: "1.1.0" });
  emit("update-not-available");
  // A download that arrives later — a manual one, or a newer build discovered by
  // the 4-hourly poll — must NOT install on the released approval.
  emit("update-downloaded", { version: "1.2.0" });
  await settle();
  assert.strictEqual(calls.quitAndInstall.length, 0);
});

test("a check or download failure releases the approval", async () => {
  const { deps, calls, emit } = makeDeps();
  const u = initAutoUpdate(deps);
  await u.installApproved({ version: "1.1.0" });
  emit("error", new Error("feed unreachable"));
  emit("update-downloaded", { version: "1.2.0" });
  await settle();
  assert.strictEqual(calls.quitAndInstall.length, 0);
});

test("a suppressed downgrade releases the approval", async () => {
  // The direction gate treats a same-channel non-newer candidate as up to date.
  // Leaving the approval armed there would install the next thing that landed.
  const { deps, calls, emit } = makeDeps({ appVersion: "2.0.0" });
  const u = initAutoUpdate(deps);
  await u.installApproved({ version: "1.9.0" });
  emit("update-available", { version: "1.9.0" });
  await settle();
  assert.strictEqual(calls.downloads, 0, "a downgrade must not be downloaded");
  emit("update-downloaded", { version: "1.9.0" });
  await settle();
  assert.strictEqual(calls.quitAndInstall.length, 0);
});

test("an approved install is refused while an install is already in flight", async () => {
  const { deps, calls, emit } = makeDeps();
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0" });
  await u.installApproved({ version: "1.1.0" });
  assert.strictEqual(calls.quitAndInstall.length, 1);
  const second = await u.installApproved({ version: "1.1.0" });
  assert.deepStrictEqual(second, { ok: false, reason: "install-in-flight" });
  assert.strictEqual(calls.quitAndInstall.length, 1);
});
