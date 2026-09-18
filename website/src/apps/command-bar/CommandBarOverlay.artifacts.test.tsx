import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import CommandBarOverlay from './CommandBarOverlay'

/**
 * The Command Bar's artifacts view.
 *
 * The bar is the DEFAULT Cmd+K surface, so the palette's Artifacts tab is
 * unreachable for anyone who has not disabled the app — a saved widget had no
 * keyboard route at all. These tests pin the route on the surface the user opens,
 * and the two properties the launcher's whole design rests on:
 *
 *  - the ROOT reaches no artifact endpoint; entering the view is what fetches,
 *  - the view searches by NAME only, so the server reads metadata rather than
 *    every artifact's body (content search is the next step).
 */

const dispatch = vi.fn()
const navigate = vi.fn()

const storeState: {
  dashboard: { slots: Record<string, unknown>[]; unreadSlots: string[] }
  chat: { slotStatusDetail: Record<string, unknown>; activeSlot: string | null }
} = {
  dashboard: { slots: [], unreadSlots: [] },
  chat: { slotStatusDetail: {}, activeSlot: null },
}

vi.mock('../../store', () => ({
  useAppDispatch: () => dispatch,
  useAppSelector: (fn: (s: unknown) => unknown) => fn(storeState),
}))
vi.mock('../../store/chatSlice', () => ({
  createSlot: (arg: unknown) => ({ type: 'createSlot', arg }),
  setPendingInput: (text: string) => ({ type: 'setPendingInput', text }),
  switchSlot: (arg: unknown) => ({ type: 'switchSlot', arg }),
  requestFolderReveal: (folderId: string) => ({ type: 'requestFolderReveal', folderId }),
}))
vi.mock('../../components/commandPalette/paletteActions', () => ({
  usePaletteActions: () => ({
    navigate,
    enterInsertOrNewSession: vi.fn(),
    newSessionWithToken: vi.fn(),
  }),
}))
vi.mock('../../components/commandPalette/providers/sessionsProvider', () => ({
  useSessionsProvider: () => ({ search: vi.fn(async () => []) }),
}))
vi.mock('../../components/commandPalette/providers/recentsProvider', async importOriginal => ({
  ...(await importOriginal<
    typeof import('../../components/commandPalette/providers/recentsProvider')
  >()),
  useRecentsProvider: () => ({ search: vi.fn(async () => []) }),
}))
vi.mock('../../hooks/useVisualViewport', () => ({ useVisualViewport: () => ({ height: 800 }) }))
vi.mock('../../hooks/useDialogFocusTrap', () => ({ useDialogFocusTrap: () => {} }))
vi.mock('../../hooks/useTheme', () => ({ useTheme: () => ({ cycle: vi.fn() }) }))

/**
 * Every network call the overlay could make, so a request is observable — and so
 * the arguments of the artifact one can be asserted, which is where the name-only
 * contract actually lives.
 */
const listApps = vi.fn(async () => [])
const chatFolders = vi.fn(async () => [])
const artifacts = vi.fn(async (_filters?: Record<string, unknown>) => ({ artifacts: [] }))
vi.mock('../../api/client', () => ({
  api: {
    listApps: (...a: unknown[]) => listApps(...(a as [])),
    chatFolders: (...a: unknown[]) => chatFolders(...(a as [])),
    artifacts: (...a: unknown[]) => artifacts(...(a as [])),
  },
}))

/** Two artifacts whose names differ, so a filtered result is distinguishable. */
const ARTIFACTS = [
  {
    slug: 'q3-revenue-chart',
    name: 'Q3 Revenue Chart',
    kind: 'widget',
    description: 'Bar chart of quarterly revenue',
    tags: [],
    version: 3,
    updated_at: '2026-09-01T00:00:00Z',
  },
  {
    slug: 'onboarding-runbook',
    name: 'Onboarding Runbook',
    kind: 'markdown',
    description: 'How a new hire gets set up',
    tags: [],
    version: 1,
    updated_at: '2026-08-01T00:00:00Z',
  },
]

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onClose = vi.fn()
  render(
    <QueryClientProvider client={client}>
      <CommandBarOverlay open onClose={onClose} />
    </QueryClientProvider>,
  )
  return { onClose, client }
}

const type = (text: string) => {
  fireEvent.change(screen.getByRole('combobox'), { target: { value: text } })
}

/**
 * The option row whose visible text contains `text`.
 *
 * Matched on the ROW rather than with `getByText` because a matched title renders
 * through `<Highlighted>`, which splits it into one element per matched run — so the
 * name is on screen without being any single text node.
 */
const rowByText = (text: string): HTMLElement => {
  const rows = screen.queryAllByRole('option')
  const hit = rows.find(r => (r.textContent || '').includes(text))
  if (!hit) {
    throw new Error(
      `no option row containing "${text}"; rows: ${rows.map(r => r.textContent).join(' | ')}`,
    )
  }
  return hit
}

/** Whether any option row shows `text` — the negative form of {@link rowByText}. */
const hasRow = (text: string): boolean =>
  screen.queryAllByRole('option').some(r => (r.textContent || '').includes(text))

/** Enter the artifacts view the way a user does: activate its root row. */
const enterView = async () => {
  await waitFor(() => expect(hasRow('Search Artifacts')).toBe(true))
  fireEvent.mouseDown(rowByText('Search Artifacts'))
  // The chip naming the view is what proves the scope was entered.
  await waitFor(() => expect(screen.getByRole('button', { name: /Back to all commands/ })).toBeTruthy())
}

beforeEach(() => {
  vi.clearAllMocks()
  artifacts.mockResolvedValue({ artifacts: [] })
  dispatch.mockReturnValue({ unwrap: () => Promise.resolve('slot-1') })
  storeState.dashboard = { slots: [], unreadSlots: [] }
  storeState.chat = { slotStatusDetail: {}, activeSlot: null }
  localStorage.clear()
})

describe('command bar — artifacts view', () => {
  it('offers the view as a command row, named as a view rather than a command', async () => {
    mount()
    await waitFor(() => expect(hasRow('Search Artifacts')).toBe(true))
    // A `view` row promises to open a surface INSIDE the bar, which is a different
    // promise from a row that acts and closes.
    expect(rowByText('Search Artifacts').textContent).toContain('View')
    expect(rowByText('Search Artifacts').textContent).not.toContain('Command')
  })

  it('reaches the row by the word the user knows a saved widget by', async () => {
    mount()
    type('widget')
    // The title has no "widget" in it; the alias is what carries the row.
    await waitFor(() => expect(hasRow('Search Artifacts')).toBe(true))
  })

  it('issues NO artifact request from the root, which is the launcher invariant', async () => {
    mount()
    type('artifact')
    await waitFor(() => expect(hasRow('Search Artifacts')).toBe(true))
    expect(artifacts).not.toHaveBeenCalled()
  })

  it('lists the newest artifacts on entering, before anything is typed', async () => {
    artifacts.mockResolvedValue({ artifacts: ARTIFACTS })
    mount()
    await enterView()
    await waitFor(() => expect(hasRow('Q3 Revenue Chart')).toBe(true))
    expect(hasRow('Onboarding Runbook')).toBe(true)
    // Entering is the activation event: the listing asks for no `q` at all.
    expect(artifacts).toHaveBeenCalledWith({ q: undefined })
  })

  it('searches by NAME only — no content scan and no snippet', async () => {
    artifacts.mockResolvedValue({ artifacts: [ARTIFACTS[0]] })
    mount()
    await enterView()
    type('revenue')
    await waitFor(() => expect(artifacts).toHaveBeenCalledWith({ q: 'revenue' }))
    // `snippet=1` alone makes the server read every listed artifact's body, so
    // leaving it on would pay for the content scan this stage does not do. Asserted
    // on the ABSENT keys because that is what the cost depends on.
    for (const call of artifacts.mock.calls) {
      expect(call[0]).not.toHaveProperty('snippet')
      expect(call[0]).not.toHaveProperty('contentMatch')
    }
  })

  it('holds the request until the query is long enough to be worth one', async () => {
    artifacts.mockResolvedValue({ artifacts: ARTIFACTS })
    mount()
    await enterView()
    await waitFor(() => expect(artifacts).toHaveBeenCalledTimes(1))
    type('r')
    // Waited PAST the field's debounce on purpose. Asserting as soon as the listing
    // rows are on screen proves nothing: they are the rows the first request already
    // returned, so the count is still 1 simply because the debounced request has not
    // had time to fire. A mutation removing the threshold survived that version of
    // this test; it only fails once the window the request would arrive in has closed.
    await new Promise(resolve => setTimeout(resolve, 400))
    // One character would return most of the corpus — which the listing already
    // shows — so it stays on the listing rather than buying a second scan.
    expect(artifacts).toHaveBeenCalledTimes(1)
    expect(hasRow('Q3 Revenue Chart')).toBe(true)
  })

  it('opens the artifact and closes the bar when its row is activated', async () => {
    artifacts.mockResolvedValue({ artifacts: ARTIFACTS })
    const { onClose } = mount()
    await enterView()
    await waitFor(() => expect(hasRow('Onboarding Runbook')).toBe(true))
    fireEvent.mouseDown(rowByText('Onboarding Runbook'))
    expect(navigate).toHaveBeenCalledWith('/artifacts/onboarding-runbook')
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('names the Enter action "Open Artifact", not "Open Session"', async () => {
    // The row shape is shared with the sessions view, so the footer is the only
    // thing that says which of the two this Enter does.
    artifacts.mockResolvedValue({ artifacts: ARTIFACTS })
    mount()
    await enterView()
    await waitFor(() => expect(screen.queryByText('Open Artifact')).not.toBeNull())
    expect(screen.queryByText('Open Session')).toBeNull()
  })

  it('offers the listing back when a name matches nothing', async () => {
    artifacts.mockResolvedValue({ artifacts: ARTIFACTS })
    mount()
    await enterView()
    artifacts.mockResolvedValue({ artifacts: [] })
    type('nothing-by-this-name')
    // A dead end with no selectable row is the state this avoids.
    await waitFor(() => expect(hasRow('show recent')).toBe(true))
    expect(rowByText('show recent').textContent).toContain('nothing-by-this-name')
  })

  it('says "No artifacts yet" on an empty corpus, not that a match failed', async () => {
    // The gap a UX review found and these tests had missed: with nothing saved and
    // nothing typed, the view reported a failed match against a query the user
    // never entered. It is the FIRST thing a new user sees here, every time, until
    // they save something.
    artifacts.mockResolvedValue({ artifacts: [] })
    mount()
    await enterView()
    await waitFor(() => expect(screen.getByRole('status').textContent).toMatch(/No artifacts yet/))
    // And it must not read as a failed search, which is the whole defect.
    expect(screen.getByRole('status').textContent).not.toMatch(/match/i)
    // No row to select, so the copy has to carry the next step itself.
    expect(screen.getByRole('status').textContent).toMatch(/save/i)
  })

  it('heads the listing "Recent" so the list says what it is', async () => {
    // Every group in the root announces itself (COMMANDS, SETTINGS). The listing
    // was unexplained rows: a reader can guess they are the recent ones, and
    // nothing on screen said so.
    artifacts.mockResolvedValue({ artifacts: ARTIFACTS })
    mount()
    await enterView()
    await waitFor(() => expect(hasRow('Q3 Revenue Chart')).toBe(true))
    expect(screen.getByText('Recent')).toBeTruthy()
  })

  it('drops the header once a name narrows the list', async () => {
    // What those rows are is the word the reader just typed, so a header there
    // would be restating the query back at them.
    artifacts.mockResolvedValue({ artifacts: ARTIFACTS })
    mount()
    await enterView()
    await waitFor(() => expect(screen.getByText('Recent')).toBeTruthy())
    artifacts.mockResolvedValue({ artifacts: [ARTIFACTS[0]] })
    type('revenue')
    // Waited for the list to actually NARROW, and for BOTH facts at once. Waiting
    // on the chart alone passes on the stale listing (it is in both the listing and
    // the result) while the debounced request is still pending. Waiting on the
    // runbook alone passes mid-flight, when the new query key has no data yet and
    // the list is briefly empty. Only both together name the settled state.
    await waitFor(() => {
      expect(hasRow('Q3 Revenue Chart')).toBe(true)
      expect(hasRow('Onboarding Runbook')).toBe(false)
    })
    expect(screen.queryByText('Recent')).toBeNull()
  })

  it('names what an artifact IS on the row, not just the category', async () => {
    // A first-time reader called them "whatever they are". The subtitle names the
    // things, which is what the settings rows in this same list do.
    mount()
    await waitFor(() => expect(hasRow('Search Artifacts')).toBe(true))
    expect(rowByText('Search Artifacts').textContent).toMatch(/Charts, documents and widgets/)
  })

  it('renders a failed search through ErrorNotice and keeps Retry keyboard-reachable', async () => {
    artifacts.mockRejectedValue(new Error('gateway down'))
    mount()
    await enterView()

    const failure = await screen.findByRole('alert')
    // The lead sentence has to say what to do. A reader shown only the rejection
    // read "gateway unavailable" as the "Run a local gateway" setting they "would
    // not dare try", so the raw text stays but can no longer be the whole notice.
    expect(failure.textContent).toContain('Search failed. Try again.')
    expect(failure.textContent).toContain('gateway down')
    // ErrorNotice stays outside the option: nesting its possible hand-off button
    // in a listbox option would give the option two competing interactions.
    expect(failure.closest('[role="option"]')).toBeNull()
    // The query is still only local combobox state, so navigating to chat would
    // discard it. The no-hand-off decision must remain visible in the rendered shape.
    expect(failure.querySelector('button')).toBeNull()

    // A failed search is not an empty one. Retry remains on the Arrow/Enter path
    // that reached the failure instead of becoming a bare button outside the list.
    const retryRow = rowByText('Retry')
    expect(retryRow.textContent).not.toContain('gateway down')
    const before = artifacts.mock.calls.length
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    await waitFor(() => expect(artifacts.mock.calls.length).toBeGreaterThan(before))
  })

  it('leaves the view on Backspace in an empty field, back to the launcher', async () => {
    artifacts.mockResolvedValue({ artifacts: ARTIFACTS })
    mount()
    await enterView()
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Backspace' })
    await waitFor(() => expect(hasRow('New Session')).toBe(true))
  })

  it('stops naming artifacts among the corpora the bar cannot reach', async () => {
    // The recovery row exists to name what is NOT searchable here. Artifacts have a
    // view of their own now, so listing them would send the reader to disable the app
    // to reach something one Enter away.
    mount()
    type('zzzz-matches-nothing')
    await waitFor(() => expect(hasRow('disable Command Bar')).toBe(true))
    const hint = rowByText('disable Command Bar').textContent || ''
    expect(hint).toContain('knowledge, skills or prompts')
    expect(hint).not.toContain('knowledge, artifacts')
  })
})
