/**
 * Screenshot of the Settings > Developer > Feature Previews card that reveals
 * the Agents page, so the PR that renamed it can show the switch rather than
 * assert its label in prose.
 *
 * Gateway-free: `openSettingsPage` serves the built SPA from loopback and
 * answers every /api/** call from fixtures, so this needs `npm run build` and
 * nothing else running.
 *
 * The frame is GATED on the rendered text. A card whose accessible name is not
 * the expected label writes no PNG and exits nonzero, so a stale capture cannot
 * be attached as evidence for a rename that did not land. The target file is
 * also deleted before the page is driven, so a failed run leaves NO frame
 * rather than leaving the previous run's.
 *
 * Usage (from website/):
 *   npm run build
 *   node scripts/capture-agent-mode-card.mjs [outDir]
 */
import { existsSync, mkdirSync, rmSync } from 'node:fs'
import path from 'node:path'

import { openSettingsPage } from './lib/settings-capture.mjs'

const OUT = path.resolve(process.argv[2] || '../temp-screenshots/agent-mode')
mkdirSync(OUT, { recursive: true })

const SHOT = path.join(OUT, 'agent-mode-card.png')
// Delete the target BEFORE driving the page, not after a failure. A run that
// fails its assertions writes nothing -- but a PREVIOUS run's frame is still
// sitting at this exact path, which is the path the PR body references, so
// "no PNG written" would silently mean "the old PNG is still your evidence".
// Removing it up front makes a failed run leave no frame at all.
//
// `force` only swallows a MISSING file, so a permission error or a directory at
// this path still throws -- and either way the old frame survives, which is the
// exact case this delete exists to prevent. Refuse loudly and name the file
// rather than letting a stack trace scroll past above a stale PNG.
try {
  rmSync(SHOT, { force: true })
} catch (err) {
  console.error(`refusing to capture: cannot remove the previous frame at ${SHOT} (${err.message})`)
  process.exit(2)
}
if (existsSync(SHOT)) {
  console.error(`refusing to capture: the previous frame is still at ${SHOT}`)
  process.exit(2)
}

const EXPECTED_LABEL = 'Agent Mode'

const { browser, context, page, srv } = await openSettingsPage({ tab: 'developer', height: 1000 })

let failed = false
function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

try {
  const toggle = page.getByRole('switch', { name: EXPECTED_LABEL, exact: true })
  await toggle.waitFor({ state: 'visible', timeout: 30_000 })

  // Climb from the switch to the nearest ancestor that actually carries the
  // card's prose. Picking a fixed div depth guesses at markup this PR does not
  // own, and an empty container silently shoots a blank frame.
  const box = await toggle.evaluate(el => {
    let node = el
    while (node.parentElement) {
      node = node.parentElement
      const t = (node.innerText || '').trim()
      if (t.length > 60) {
        const r = node.getBoundingClientRect()
        return { text: t, x: r.x, y: r.y, width: r.width, height: r.height }
      }
    }
    return null
  })
  if (!box) throw new Error('no ancestor of the switch carries the card prose')

  const text = box.text.replace(/\s+/g, ' ')

  const ok = check(
    'card label',
    text.startsWith(EXPECTED_LABEL),
    `text="${text.slice(0, 90)}"`,
  )
  check('names the Agents page', /\bAgents page\b/.test(text), `text="${text.slice(0, 120)}"`)
  check('no stale noun', !/Crew Members/i.test(text), 'card must not say "Crew Members"')

  if (ok && !failed) {
    const pad = 8
    await page.screenshot({
      path: SHOT,
      clip: {
        x: Math.max(0, box.x - pad),
        y: Math.max(0, box.y - pad),
        width: box.width + pad * 2,
        height: box.height + pad * 2,
      },
    })
    console.log(`wrote ${SHOT}`)
  } else {
    console.log('assertions failed - no PNG written, and any earlier frame was removed')
  }
} finally {
  await context.close()
  await browser.close()
  srv.close()
}

process.exit(failed ? 1 : 0)
