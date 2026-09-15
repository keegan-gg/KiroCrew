/** Real-browser evidence for the EXPANDED view's MINT-FAILURE overlay.
 *
 * Drives website/capture/crew-webview-expanded-mint-error.html, which mounts the
 * REAL `CrewWebview` over a gateway that mints once and then refuses. The script
 * walks the real sequence: expand (mint lands, document renders), switch theme
 * (`srcdoc` changes, so the hook re-mints), and the refusal raises the failure
 * bar over the document the first mint produced. That is the shot the UX review
 * asked for -- it is what shows whether the bar stays legible over crew content.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6824 --strictPort    # in another shell (website/)
 *   node scripts/capture-crew-webview-expanded-mint-error.mjs http://127.0.0.1:6824 ../temp-screenshots/crew-webview
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6824'
const OUT = process.argv[3] || '../temp-screenshots/crew-webview'
mkdirSync(OUT, { recursive: true })

// The expanded view is `fixed inset-0`, so the viewport IS the shot.
const VIEWPORT = { width: 1120, height: 720 }

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
}

const page = await browser.newPage({ viewport: VIEWPORT, deviceScaleFactor: 2 })
page.on('pageerror', e => {
  console.error('pageerror:', e.message)
  failures++
})
// Starts LIGHT so the switch lands on DARK, which is the theme the stand-in
// document is drawn in. Both directions are real, but a light chrome over a dark
// document adds a second variable to a shot that exists to answer one question,
// and a reader would reasonably wonder whether the mismatch is the defect.
await page.goto(`${BASE}/capture/crew-webview-expanded-mint-error.html?theme=light`, {
  waitUntil: 'domcontentloaded',
})

const expand = page.locator('[data-testid="crew-webview-expand"]')
await expand.waitFor({ state: 'visible', timeout: 15000 })
await expand.click()

const frame = page.locator('[data-expanded="true"] iframe')
await frame.waitFor({ state: 'visible', timeout: 15000 })
check('first mint renders the document', await frame.isVisible())
// Read INSIDE the frame: a visible iframe element proves nothing about whether a
// document painted, and an empty frame would make the legibility question
// unanswerable while the shot still looked plausible.
const inner = page.frameLocator('[data-expanded="true"] iframe').locator('body')
await inner.waitFor({ state: 'visible', timeout: 15000 })
check('the document actually painted', ((await inner.textContent()) || '').includes('Sources read'))

// The real trigger for a second mint: a theme change rebuilds `srcdoc`. The
// switch is a harness control parked outside the viewport so it cannot obstruct
// the shot, so dispatch the click rather than asking Playwright to reach it.
await page.locator('[data-testid="capture-theme-switch"]').dispatchEvent('click')

const bar = page.locator('[data-testid="crew-webview-mint-error"]')
await bar.waitFor({ state: 'visible', timeout: 15000 })
check('the refused re-mint raises the failure bar', await bar.isVisible())
// The whole point: the bar sits OVER a document rather than replacing it.
check('the document is still behind the bar', await frame.isVisible())
check(
  'the bar offers a way back',
  await page.locator('[data-expanded="true"] button', { hasText: 'Try again' }).isVisible(),
)

await page.waitForTimeout(200)
await page.screenshot({ path: `${OUT}/07-expanded-mint-error.png` })

await page.close()
await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}/07-expanded-mint-error.png`)
