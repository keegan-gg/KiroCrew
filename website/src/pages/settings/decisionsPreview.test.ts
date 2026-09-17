/**
 * `readDecisions` — the config shapes the card has to survive, plus the
 * cross-layer drift guard on the one config path it writes.
 *
 * The reader is a pure function precisely so these cases cost nothing to pin:
 * the interesting states are all "the config is not what this build expects",
 * and each one has a different correct answer. The two shapes that matter most
 * are a gateway with no `decisions.enabled` field (older than this switch) and
 * one carrying only the retired `decisions.preview` — both must read as
 * unsupported, never as off, and never as consent.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, it, expect } from 'vitest'

import { SETTINGS_REGISTRY } from '../../components/commandPalette/settingsRegistry.gen'
import {
  DECISIONS_BUCKET_PATH,
  DECISIONS_ENABLED_PATH,
  DECISIONS_LIVE_POINT,
  readDecisions,
} from './decisionsPreview'

describe('readDecisions', () => {
  it('reads a config with no decisions section as unsupported, never as off', () => {
    // The distinction is the whole point: "off" invites a click, "unsupported"
    // means the write would come back 400 from a gateway that has no such field.
    const view = readDecisions({ telemetry: { enabled: true } })
    expect(view.supported).toBe(false)
    expect(view.enabled).toBe(false)
    expect(view.bucket).toBeNull()
  })

  it('reads an unresolved or failed config query as unsupported', () => {
    for (const value of [undefined, null, 'nope', 42, []]) {
      expect(readDecisions(value).supported).toBe(false)
    }
  })

  it('reads a section carrying only the retired preview flag as unsupported', () => {
    // A shadow-era gateway. It HAS a `decisions` section, so keying support off
    // the section's presence would offer a write its allowlist refuses.
    const view = readDecisions({ decisions: { preview: false, points: {} } })
    expect(view.supported).toBe(false)
    expect(view.enabled).toBe(false)
  })

  it('never reads the retired preview flag as consent for this switch', () => {
    // `preview: true` was consent to a seam that logged an answer and discarded
    // it. This switch lets the answer pick skills, which is a different question
    // and needs its own yes.
    const view = readDecisions({ decisions: { preview: true, points: {} } })
    expect(view.supported).toBe(false)
    expect(view.enabled).toBe(false)
  })

  it('reads a present field with the flag off as supported', () => {
    const view = readDecisions({ decisions: { enabled: false } })
    expect(view.supported).toBe(true)
    expect(view.enabled).toBe(false)
  })

  it('reads a present-but-unreadable flag as supported and off', () => {
    // The FIELD is what says this gateway understands the switch; the VALUE is
    // what says whether it is on. A sloppy value is a value, so the switch is
    // offered — showing off, which is what the backend's own coercion resolves.
    const view = readDecisions({ decisions: { enabled: 'nope' } })
    expect(view.supported).toBe(true)
    expect(view.enabled).toBe(false)
  })

  it('turns on for an exact true only', () => {
    expect(readDecisions({ decisions: { enabled: true } }).enabled).toBe(true)
    // A hand-edited truthy value is not consent to send message text off the
    // machine, so it reads as off rather than as "they probably meant yes".
    for (const sloppy of ['true', 1, 'yes', {}]) {
      expect(readDecisions({ decisions: { enabled: sloppy } }).enabled).toBe(false)
    }
  })

  it('reports a sampling rate that narrows the sessions the point fires for', () => {
    expect(readDecisions({ decisions: { enabled: true, bucket: 25 } }).bucket).toBe(25)
    // Zero is REPORTED, not dropped: "on, and sampling nobody" is otherwise only
    // discoverable by waiting for a log line that never arrives.
    expect(readDecisions({ decisions: { enabled: true, bucket: 0 } }).bucket).toBe(0)
  })

  it('reports nothing for a rate that says no more than the row already does', () => {
    // 100 is every session, which is what an on row means on its own.
    expect(readDecisions({ decisions: { enabled: true, bucket: 100 } }).bucket).toBeNull()
    // Absent: an older section, or one an operator trimmed. The backend's
    // default decides, and this reader must not print a number it invented.
    expect(readDecisions({ decisions: { enabled: true } }).bucket).toBeNull()
  })

  it('reports nothing for a rate this build cannot read as a percentage', () => {
    // Out of range, fractional or the wrong type: the backend clamps or coerces
    // these, and a card that guessed which way would state a rate that is not
    // in force.
    for (const bad of [-1, 101, 500, 12.5, '25', null, {}, NaN, Infinity]) {
      expect(readDecisions({ decisions: { enabled: true, bucket: bad } }).bucket).toBeNull()
    }
  })

  it('names the config paths and the one point this release acts on', () => {
    expect(DECISIONS_ENABLED_PATH).toBe('decisions.enabled')
    expect(DECISIONS_BUCKET_PATH).toBe('decisions.bucket')
    // Singular on purpose: `skills.dedupe` and `cron.novelty` shipped as rows in
    // the shadow release and are retired here, because a row for an answer
    // nothing consumes described a comparison rather than a thing being on.
    expect(DECISIONS_LIVE_POINT).toBe('skills.select')
  })
})

/**
 * The `configKey` prop is a duplicated string, so something has to hold the copy
 * to the constant.
 *
 * `scripts/settingsExtract.ts` reads props out of source TEXT: `configKey={PATH}`
 * extracts as nothing at all, which silently drops the key from the registry
 * entry and degrades a `<SettingRef>` chip to a CLI popover while a real toggle
 * exists. Widening the extractor to resolve an identifier is deliberately not the
 * fix — it is a text scanner by design — so the literal stays, and these two
 * assertions are what stop it drifting from the constant the mutation writes.
 *
 * The backend half of the same guard (this key exists in `SCHEMA_REGISTRY`, and
 * the point name matches the backend's own constant) lives in
 * `test/test_settingref_schema_fixture.py`.
 */
describe('the Decisions toggle configKey', () => {
  // `__dirname`, not `import.meta.url`: under vitest the module URL is not a
  // file: URL, so `readFileSync` on it throws before any assertion runs.
  const source = readFileSync(resolve(__dirname, 'FeaturePreviewsSection.tsx'), 'utf-8')

  it('is spelled as the literal the extractor can see', () => {
    expect(source).toContain(`configKey="${DECISIONS_ENABLED_PATH}"`)
    // The identifier form is the failure this guards, so it must not appear.
    expect(source).not.toContain('configKey={DECISIONS_ENABLED_PATH}')
  })

  it('reaches the generated registry as that same key', () => {
    const entry = SETTINGS_REGISTRY.find(
      e => e.labelKey === 'pages.developer.featurePreviewsTab.decisions',
    )
    expect(entry).toBeDefined()
    expect(entry?.configKey).toBe(DECISIONS_ENABLED_PATH)
  })
})
