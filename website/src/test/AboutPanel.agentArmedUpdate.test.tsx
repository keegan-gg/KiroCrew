// The desktop app's approval of an agent-armed update (issue #503).
//
// Contract under test:
// - the notice appears ONLY for an arm on the app lane, and only on a shell
//   whose bridge exposes the two step-up calls; an older shell renders nothing
// - it names the version, because a consent that does not say what it installs
//   is not consent
// - the click reports a RESOLVED `{ ok: false, error }` refusal as an error,
//   which is the bridge's contract and not a rejection
// - the notice stays mounted through the handoff: the approval consumes the arm,
//   so the very next poll says `armed: false`
// - a managed-venv arm must NOT summon it, or the button would drive the wrong
//   updater
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { AgentArmedUpdateNotice } from '../pages/settings/AboutPanel'

type ArmedStatus = Awaited<ReturnType<NonNullable<UpdateAPI['armedStatus']>>>

function bridge(
  status: ArmedStatus,
  approve: { ok: boolean; version?: string; error?: string } | Error = { ok: true, version: '0.6.0' },
) {
  const armedStatus = vi.fn(async () => status)
  const approveArmed = vi.fn(async () => {
    if (approve instanceof Error) throw approve
    return approve
  })
  return { armedStatus, approveArmed } as unknown as UpdateAPI & {
    armedStatus: typeof armedStatus
    approveArmed: typeof approveArmed
  }
}

function renderNotice(api?: UpdateAPI) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <AgentArmedUpdateNotice api={api} />
    </QueryClientProvider>,
  )
}

const APP_ARM: ArmedStatus = {
  armed: true,
  version: '0.6.0',
  channel: 'stable',
  managed_by: 'electron',
  expires_in: 305,
  request_id: 'r1',
}

describe('AgentArmedUpdateNotice', () => {
  beforeEach(() => {
    vi.stubGlobal('window', window)
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    delete (window as { updateAPI?: unknown }).updateAPI
  })

  it('renders nothing on a shell whose bridge predates the step-up calls', async () => {
    // An older desktop build has no armedStatus/approveArmed. Assuming one would
    // put a dead button in front of a channel that does not exist.
    const { container } = renderNotice({ check: vi.fn(), install: vi.fn(), onState: vi.fn() } as unknown as UpdateAPI)
    await waitFor(() => expect(container.textContent).toBe(''))
    expect(screen.queryByTestId('agent-armed-update')).toBeNull()
  })

  it('renders nothing when nothing is armed', async () => {
    const api = bridge({ armed: false })
    renderNotice(api)
    await waitFor(() => expect(api.armedStatus).toHaveBeenCalled())
    expect(screen.queryByTestId('agent-armed-update')).toBeNull()
  })

  it('names the version and shows the remaining window', async () => {
    renderNotice(bridge(APP_ARM))
    const panel = await screen.findByTestId('agent-armed-update')
    // A consent prompt that does not say WHICH version it installs is not consent.
    expect(panel.textContent).toContain('0.6.0')
    expect((await screen.findByTestId('agent-armed-countdown')).textContent).toContain('5:05')
  })

  it('does not summon itself for a managed-venv arm', async () => {
    // That lane is approved with the host command InAppUpdateFlow prints; an
    // app-install button here would drive the updater that does not own it.
    const api = bridge({ ...APP_ARM, managed_by: 'kirocrew' })
    renderNotice(api)
    await waitFor(() => expect(api.armedStatus).toHaveBeenCalled())
    expect(screen.queryByTestId('agent-armed-update')).toBeNull()
  })

  it('approves on click and stays mounted while the app hands off', async () => {
    const api = bridge(APP_ARM)
    renderNotice(api)
    fireEvent.click(await screen.findByTestId('agent-armed-approve'))
    await waitFor(() => expect(api.approveArmed).toHaveBeenCalledTimes(1))
    // The approval consumed the arm, so the next poll answers armed:false.
    // Unmounting there would blank the panel at the moment the app goes quiet.
    await screen.findByTestId('agent-armed-installing')
    expect(screen.queryByTestId('agent-armed-approve')).toBeNull()
  })

  it('surfaces a resolved refusal, which is the bridge contract', async () => {
    // `{ ok: false, error }` RESOLVES. Treating only a rejection as failure
    // leaves the button looking like it worked while nothing installs.
    const api = bridge(APP_ARM, { ok: false, error: 'the armed request has expired' })
    renderNotice(api)
    fireEvent.click(await screen.findByTestId('agent-armed-approve'))
    const error = await screen.findByTestId('agent-armed-error')
    expect(error.textContent).toContain('expired')
    // Still actionable: the user can arm again and retry.
    expect(screen.queryByTestId('agent-armed-approve')).not.toBeNull()
  })

  it('surfaces a rejecting bridge call too', async () => {
    const api = bridge(APP_ARM, new Error('ipc gone'))
    renderNotice(api)
    fireEvent.click(await screen.findByTestId('agent-armed-approve'))
    const error = await screen.findByTestId('agent-armed-error')
    expect(error.textContent?.length).toBeGreaterThan(0)
  })

  it('reads window.updateAPI when no api prop is given', async () => {
    const api = bridge(APP_ARM)
    ;(window as { updateAPI?: unknown }).updateAPI = api
    renderNotice()
    await screen.findByTestId('agent-armed-update')
  })
})
