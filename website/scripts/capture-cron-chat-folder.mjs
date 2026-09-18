/**
 * Screenshots for the Schedule form's chat-folder picker (#1620).
 *
 * Drives the ISOLATED capture entry (website/capture/cron-chat-folder.html),
 * which mounts the REAL JobForm with `/api/chat/folders` answered by a fixture.
 *
 * Three frames, one per thing a reader has to be able to see:
 *  - closed: the control in its default state, with its own hint, on a new job.
 *  - open:   the folder list, where a nested folder reads as `Work / Standups`.
 *            Captured by CLICKING the real trigger, so the frame documents the
 *            shipped control rather than a forced state.
 *  - filed:  an existing job opening on the folder it is already filed in — the
 *            read side, whose absence would silently unfile a job on the next
 *            unrelated save.
 *
 * The open frame also ASSERTS the path label, because a screenshot of a list
 * showing two bare `Standups` rows would look fine and be the defect.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6813 --strictPort   # in another shell
 *   node scripts/capture-cron-chat-folder.mjs http://127.0.0.1:6813 ../temp-screenshots/1620-cron-chat-folder
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6813'
const OUT = process.argv[3] || '../temp-screenshots/1620-cron-chat-folder'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 760, height: 900 }
/** The nested label the picker must show, not a bare duplicate folder name. */
const NESTED_LABEL = 'Work \u203a Standups'
/**
 * Radix's popup enters with `fade-in-0 zoom-in-95`, so the panel is still
 * translucent and mid-scale for ~150ms after its content is queryable. A shot
 * taken the moment `waitFor` resolves therefore shows the list bleeding into the
 * controls behind it, which is exactly the frame a blind reader cannot parse.
 * Settle on the animation ENDING rather than on a sleep, so a slower runner does
 * not silently go back to catching it mid-flight.
 */
const POPUP_SETTLE_MS = 400

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

for (const scene of ['closed', 'open', 'filed']) {
  const page = await browser.newPage({ viewport: VIEWPORT })
  await page.goto(`${BASE}/capture/cron-chat-folder.html?theme=dark&scene=${scene === 'open' ? 'closed' : scene}`, {
    waitUntil: 'networkidle',
  })
  const trigger = page.getByLabel('Chat folder')
  await trigger.waitFor()

  if (scene === 'open') {
    await trigger.click()
    const nested = page.getByText(NESTED_LABEL, { exact: true })
    try {
      await nested.waitFor({ timeout: 5000 })
      // Opacity is the readable signal: the entry keyframe animates it to 1, so
      // waiting on it is waiting on the animation rather than on a guess.
      await page
        .locator('[role="listbox"], [data-radix-select-content]')
        .first()
        .evaluate(el =>
          Promise.all(el.getAnimations({ subtree: true }).map(a => a.finished.catch(() => {}))),
        )
      await page.waitForTimeout(POPUP_SETTLE_MS)
      const opacity = await page
        .locator('[role="listbox"], [data-radix-select-content]')
        .first()
        .evaluate(el => Number(getComputedStyle(el).opacity))
      if (!(opacity >= 0.99)) {
        failures++
        console.error(`FAIL open: panel still at opacity ${opacity} -- frame is mid-transition`)
      }
    } catch (err) {
      failures++
      console.error(`FAIL open: the list never showed "${NESTED_LABEL}" (${err})`)
    }
  } else if (scene === 'filed') {
    // The read side: wait for the folder query to resolve into the trigger,
    // or the frame documents an empty picker on a filed job.
    try {
      await page.waitForFunction(
        () => (document.querySelector('[aria-label="Chat folder"]')?.textContent || '').includes('Standups'),
        { timeout: 5000 },
      )
    } catch {
      failures++
      console.error('FAIL filed: the trigger never showed the job\'s saved folder')
    }
  }

  await page.screenshot({ path: `${OUT}/${scene}.png`, fullPage: scene !== 'open' })
  console.log(`wrote ${OUT}/${scene}.png`)
  await page.close()
}

await browser.close()
process.exit(failures ? 1 : 0)
