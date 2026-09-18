/**
 * Screenshots of the agent-armed update notice in Settings > About (issue #503).
 *
 *   none        a packaged install with nothing armed — the BEFORE frame. The
 *               notice must be ABSENT, which is what makes the pair meaningful.
 *   armed       an agent armed v0.6.0: the notice names the version and the
 *               remaining approval window, and offers the human's install click.
 *   installing  one real click later: the arm is consumed and the app is handing
 *               off. The panel must still be on screen.
 *
 * Drives the ISOLATED capture entry (website/capture/agent-armed-update.html);
 * see that file for why the full SPA is not used. Each scene asserts a marker and
 * the script EXITS NONZERO when one is missing, so it can never quietly emit a
 * screenshot of the wrong state.
 *
 *   npx vite --host 127.0.0.1 --port 6814 --strictPort      # in another shell
 *   node scripts/capture-agent-armed-update.mjs http://127.0.0.1:6814 <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6814'
const OUT = process.argv[3] || '../temp-screenshots/agent-armed-update'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  {
    file: 'about-nothing-armed-before.png',
    scene: 'none',
    // The About panel renders normally; the notice is not there at all.
    marker: 'text=Check for updates',
    absent: ['[data-testid="agent-armed-update"]'],
  },
  {
    file: 'about-agent-armed-after.png',
    scene: 'armed',
    marker: '[data-testid="agent-armed-update"]',
    alsoVisible: [
      'text=An agent requested an update',
      // A consent prompt that does not name the version is not consent.
      'text=0.6.0',
      '[data-testid="agent-armed-countdown"]',
      '[data-testid="agent-armed-approve"]',
    ],
    absent: [],
  },
  {
    file: 'about-agent-armed-installing-after.png',
    scene: 'installing',
    marker: '[data-testid="agent-armed-installing"]',
    // The button is gone: the install is a one-way door.
    absent: ['[data-testid="agent-armed-approve"]'],
  },
]

const b = await chromium.launch()
let failed = 0
for (const s of SCENES) {
  const page = await b.newPage({ viewport: { width: 900, height: 1000 } })
  const url = `${BASE}/capture/agent-armed-update.html?scene=${s.scene}&theme=dark&lang=en`
  try {
    await page.goto(url, { waitUntil: 'networkidle' })
    await page.waitForSelector(s.marker, { timeout: 10000 })
    for (const sel of s.alsoVisible || []) {
      await page.waitForSelector(sel, { timeout: 10000 })
    }
    for (const sel of s.absent || []) {
      if (await page.locator(sel).count()) {
        throw new Error(`expected ${sel} to be absent in scene ${s.scene}`)
      }
    }
    await page.screenshot({ path: `${OUT}/${s.file}`, fullPage: true })
    console.log(`ok   ${s.file}`)
  } catch (err) {
    failed += 1
    console.error(`FAIL ${s.file}: ${(err && err.message) || err}`)
  }
  await page.close()
}
await b.close()
process.exit(failed ? 1 : 0)
