/**
 * Isolated capture entry for the agent-armed update notice in Settings > About
 * (issue #503).
 *
 * WHY ISOLATED: the state being photographed is a property of two things a
 * browser session cannot reach. It needs `window.updateAPI` — which exists only
 * inside the packaged Electron shell — and it needs the gateway to be holding an
 * ARMED step-up request that an agent created. So the desktop bridge and the
 * gateway's arm projection are stubbed and everything else is real: the REAL
 * AboutPanel, the REAL stylesheet and theme tokens, and the same payload shapes
 * the main process and the gateway actually emit.
 *
 * Scenes (?scene=):
 *   none        a packaged install with nothing armed. This is what the panel has
 *               always looked like, and it is the BEFORE frame — the notice must
 *               be absent, not merely empty.
 *   armed       an agent armed v0.6.0. The notice names the version and the
 *               remaining approval window, and the install button is the human's.
 *   installing  the same panel one click later: the arm is consumed and the app
 *               is handing off. The panel must stay on screen here, because this
 *               is the moment the dashboard goes quiet.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

// Initialise i18next exactly as main.tsx does: importing the module only DEFINES
// initI18n, and without calling it every label in the frame renders blank.
import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { sseStatus } from '../src/store/dashboardSlice'
import { AboutPanel } from '../src/pages/settings/AboutPanel'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'armed'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme)

/** The gateway's nonce-free projection of the armed request. */
const ARMED = {
  armed: true,
  request_id: 'a1b2c3d4',
  version: '0.6.0',
  channel: 'stable',
  managed_by: 'electron',
  expires_in: 305,
  approve_command: 'kirocrew update approve',
}

// `installing` is reached by CLICKING, not by a different stub: the panel's
// post-approval state is a consequence of the approval resolving ok, and a scene
// that faked it directly would photograph a state the code cannot produce.
const autoApprove = scene === 'installing'
const armedNow = scene !== 'none'

const noop = async () => ({ ok: true })
;(window as unknown as { updateAPI?: unknown }).updateAPI = {
  onState: () => () => {},
  check: noop,
  download: noop,
  install: noop,
  getInfo: async () => ({
    version: '0.5.0',
    channel: 'stable',
    stampedChannel: 'stable',
    channelSwitchable: true,
    channelPreference: 'stable',
    platform: 'darwin-arm64',
    packaged: true,
    autoDownload: true,
    laneVersion: '0.6.0',
    runningAheadOfLane: false,
    downloadUrl: 'https://download.crew.kiro.dev/desktop/stable/latest',
  }),
  setChannel: noop,
  setAutoDownload: noop,
  armedStatus: async () => (armedNow ? ARMED : { armed: false }),
  approveArmed: async () => ({ ok: true, version: '0.6.0' }),
}

store.dispatch(sseStatus({
  uptime: '4h',
  sessions: 2,
  messages: 0,
  cron_jobs: 0,
  lessons: 0,
  version: '0.5.0',
  version_display: '0.5.0',
  release_channel: 'stable',
  // A packaged install: the gateway defers its own verdict to the app's updater.
  update_managed_by: 'electron',
  update_can_apply: false,
  update_can_arm: false,
  update_check_status: 'deferred',
} as never))

// The panel's own fetches: a capture page has no gateway behind it, and an
// unanswered /api/update/check would leave the card in its loading state.
const realFetch = window.fetch
window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(typeof input === 'string' ? input : (input as Request).url ?? input)
  if (url.includes('/api/')) {
    return new Response(JSON.stringify({ auto_update: false }), {
      status: 200, headers: { 'content-type': 'application/json' },
    })
  }
  return realFetch(input, init)
}) as typeof window.fetch

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/settings']}>
        <div
          style={{ background: 'var(--bg)', color: 'var(--text)', padding: 24 }}
          data-capture-root
        >
          <div style={{ maxWidth: 760 }}>
            <AboutPanel />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)

if (autoApprove) {
  // Drive the real click once the notice has mounted, so the frame shows the
  // state the component actually reaches rather than one a stub asserted.
  const tick = setInterval(() => {
    const button = document.querySelector<HTMLButtonElement>('[data-testid="agent-armed-approve"]')
    if (!button) return
    clearInterval(tick)
    button.click()
  }, 50)
}
