/**
 * The `decisions.enabled` flag — read side, as a pure function over the config.
 *
 * Unlike the other Feature Previews in `FeaturePreviewsSection.tsx`, this one is
 * NOT a per-device `previewFlags.ts` key. The gate that acts on it runs in the
 * backend, which cannot read this browser's localStorage — so the flag has to be
 * a `config.json` value, written through `PATCH /api/config/kirocrew` like the
 * telemetry switch on the Privacy panel.
 *
 * A backend that predates the `decisions.enabled` field answers the config GET
 * without it, and its PATCH allowlist refuses the write. That is a real state of
 * this dashboard — the frontend ships ahead of the gateway it talks to whenever a
 * user updates one half first — so `supported` is derived rather than assumed,
 * and the card disables its switch instead of offering a write that returns 400.
 *
 * WHAT THE FLAG NOW BUYS, because the copy and these rules follow it: an enabled,
 * sampled session can let Jev pick the automatic skill for a message instead of
 * the word-overlap rule, and a timeout, refusal or invalid answer keeps that rule.
 * `decisions.bucket` is the share of sessions admitted. See
 * `docs/system-specs/modules/decisions.md`.
 *
 * `decisions.preview` — the field this switch wrote before that — is DELIBERATELY
 * not read here, not even as a fallback. It armed a seam that only recorded an
 * answer; this one lets the answer decide which skill a message loads. Carrying
 * the old value forward would read consent to an observation as consent to a
 * behaviour change, so a gateway exposing only `preview` is reported as
 * unsupported and the switch stays shut.
 */

/** The config path the card's switch writes. */
export const DECISIONS_ENABLED_PATH = 'decisions.enabled'

/** The config path the sampling rate is read from. Never written from here. */
export const DECISIONS_BUCKET_PATH = 'decisions.bucket'

/**
 * The ONE decision point whose answer this release acts on.
 *
 * Hard-coded rather than enumerated from the config, and singular rather than the
 * three-row table the shadow release printed: the two retired points logged an
 * answer nothing consumed, so a row for them described a comparison, not
 * something the operator was turning on. The name is backend vocabulary and is
 * held to the backend's own constant by `test/test_settingref_schema_fixture.py`.
 */
export const DECISIONS_LIVE_POINT = 'skills.select'

/** Bounds the backend clamps the sampling bucket to, restated for the reader. */
const BUCKET_MIN = 0
const BUCKET_MAX = 100

export interface DecisionsView {
  /** Whether this gateway's config carries the `decisions.enabled` field. */
  supported: boolean
  /** The stored flag. Only an exact `true` reads as on. */
  enabled: boolean
  /**
   * Sampling percentage worth PRINTING, or `null` when there is nothing to say.
   *
   * `null` covers four different configs that all mean "do not print a rate":
   * the field is absent (an older or hand-trimmed section), it is not a whole
   * number in 0–100 (so the backend's own clamp decides, and this reader must
   * not guess which way), or it is exactly 100 — every session, which is what
   * the row already says by reading as on.
   */
  bucket: number | null
}

const UNSUPPORTED: DecisionsView = { supported: false, enabled: false, bucket: null }

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

/**
 * The sampling rate, or `null` when the config gives nothing printable.
 *
 * A percentage of 0 IS printable and is kept: "on, and sampling nobody" is a
 * state an operator can otherwise only discover by waiting for a log line that
 * never comes.
 */
function readBucket(decisions: Record<string, unknown>): number | null {
  const raw = decisions.bucket
  if (typeof raw !== 'number' || !Number.isInteger(raw)) return null
  if (raw < BUCKET_MIN || raw >= BUCKET_MAX) return null
  return raw
}

/**
 * Read the flag and the sampling rate out of a `GET /api/config/kirocrew` body.
 *
 * `undefined` — the query has not resolved, or it failed — reads as unsupported,
 * which is also how the card renders it: a switch offered against a config the
 * dashboard has not read yet would be guessing at its own current state.
 *
 * Only an exact `true` turns the preview on. A hand-edited `"true"` or `1` reads
 * as off, because this is an opt-in that sends message text off the machine and
 * lets the answer pick skills, and a sloppy value is not consent.
 */
export function readDecisions(config: unknown): DecisionsView {
  const root = asRecord(config)
  if (!root) return UNSUPPORTED
  const decisions = asRecord(root.decisions)
  if (!decisions) return UNSUPPORTED
  // The FIELD, not the section, is what makes this switch writable: a shadow-era
  // gateway carries a `decisions` section with `preview` in it and would still
  // refuse this write.
  if (!('enabled' in decisions)) return UNSUPPORTED

  return {
    supported: true,
    enabled: decisions.enabled === true,
    bucket: readBucket(decisions),
  }
}
