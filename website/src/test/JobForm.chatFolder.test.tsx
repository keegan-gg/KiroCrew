/** Tests for the job form's chat-folder picker (issue #1620).
 *
 *  The setting decides where a recurring job's RUNS land in the chat sidebar, so
 *  the properties that matter are: the reader can tell two same-named folders
 *  apart before picking one; an existing job's placement round-trips instead of
 *  being cleared by the next unrelated save; and clearing it actually reaches the
 *  backend, because "" is the real value for "do not file" and an omitted field
 *  on a PATCH would make the clear a no-op.
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from './helpers'
import JobForm, { buildBody, parseJobDefaults } from '../components/JobForm'
import type { ChatFolder, CronJob } from '../types'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    updateCron: vi.fn(),
    createCron: vi.fn(),
    models: vi.fn().mockResolvedValue({ models: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    chatFolders: vi.fn(),
  },
}))

const FOLDERS: ChatFolder[] = [
  { id: 'work', name: 'Work', order: 0 },
  { id: 'standups', name: 'Standups', order: 0, parent_id: 'work' },
  { id: 'home', name: 'Home', order: 1 },
]

function makeJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'cf1', name: 'standup brief', message: 'Write the brief.', schedule: '', enabled: true,
    every_secs: 3600, ...overrides,
  } as CronJob
}

beforeEach(() => {
  vi.mocked(api.chatFolders).mockResolvedValue(FOLDERS)
})

describe('the chat-folder picker', () => {
  it('offers every sidebar folder, a nested one by its full path', async () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    await userEvent.click(await screen.findByLabelText('Chat folder'))
    // The full path, not an indent: a select shows one row at a time with its
    // siblings hidden, so depth alone would not say which "Standups" this is.
    expect(await screen.findByText('Work › Standups')).toBeInTheDocument()
    expect(screen.getByText('Work')).toBeInTheDocument()
    expect(screen.getByText('Home')).toBeInTheDocument()
  })

  it('opens on "do not file runs" for a new job', async () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    expect(await screen.findByLabelText('Chat folder')).toHaveTextContent('Do not file runs')
  })

  it('explains what filing does without claiming it replaces delivery', async () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    expect(await screen.findByText(/Notifications and Slack delivery are unchanged/)).toBeVisible()
  })

  it('sends the picked folder on create', async () => {
    const onSaved = vi.fn()
    vi.mocked(api.createCron).mockResolvedValue({})
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={onSaved} />)
    await userEvent.type(await screen.findByLabelText('Name'), 'Standup brief')
    await userEvent.type(screen.getByLabelText('Message'), 'Write the brief.')
    await userEvent.click(await screen.findByLabelText('Chat folder'))
    await userEvent.click(await screen.findByText('Work › Standups'))
    await userEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.createCron).toHaveBeenCalledWith(
      expect.objectContaining({ chat_folder_id: 'standups' }),
    )
  })

  it('shows an existing job as filed where it is filed', async () => {
    renderWithProviders(
      <JobForm layout="vertical" agents={[]} onSaved={() => {}} job={makeJob({ chat_folder_id: 'home' })} />,
    )
    const picker = await screen.findByLabelText('Chat folder')
    await waitFor(() => expect(picker).toHaveTextContent('Home'))
  })

  it('carries the unchanged placement through an unrelated edit', async () => {
    // Without the read side the picker would open empty and this save would
    // silently unfile the job.
    const onSaved = vi.fn()
    vi.mocked(api.updateCron).mockResolvedValue({})
    renderWithProviders(
      <JobForm layout="vertical" agents={[]} onSaved={onSaved} job={makeJob({ chat_folder_id: 'home' })} />,
    )
    await userEvent.type(await screen.findByLabelText('Name'), ' v2')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.updateCron).toHaveBeenCalledWith(
      'cf1',
      expect.objectContaining({ chat_folder_id: 'home' }),
    )
  })

  it('sends the empty value when the reader clears it, so the clear lands', async () => {
    const onSaved = vi.fn()
    vi.mocked(api.updateCron).mockResolvedValue({})
    renderWithProviders(
      <JobForm layout="vertical" agents={[]} onSaved={onSaved} job={makeJob({ chat_folder_id: 'home' })} />,
    )
    await userEvent.click(await screen.findByLabelText('Chat folder'))
    await userEvent.click(await screen.findByText('Do not file runs'))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.updateCron).toHaveBeenCalledWith(
      'cf1',
      expect.objectContaining({ chat_folder_id: '' }),
    )
  })

  it('defaults to unfiled for a new job', () => {
    expect(parseJobDefaults(undefined).chatFolderId).toBe('')
  })

  it('reads the existing setting rather than defaulting over it', () => {
    expect(parseJobDefaults(makeJob({ chat_folder_id: 'work' })).chatFolderId).toBe('work')
  })

  it('never confuses the placement with the Schedule-page grouping', () => {
    // folder_id groups the job's ROW on the Schedule page; chat_folder_id decides
    // where its RUNS land. Reading one off the other would move the wrong thing.
    const defaults = parseJobDefaults(makeJob({ folder_id: 'sched9' }))
    expect(defaults.chatFolderId).toBe('')
  })

  it('sends no placement for a script job, which never gets a chat session', () => {
    // A script cron takes no agent turn and creates no slot, so a folder it
    // named could never receive anything. Storing the setting anyway is how a
    // setting starts lying -- the same reason minimal_context is omitted there.
    const body = buildBody(
      { ...parseJobDefaults(makeJob({ script: 'a.py:run' })), chatFolderId: 'home' },
      'UTC',
      () => {},
      true,
    )
    expect(body?.chat_folder_id).toBe('')
  })

  it('does not offer the picker on a script job at all', async () => {
    renderWithProviders(
      <JobForm layout="vertical" agents={[]} onSaved={() => {}} job={makeJob({ script: 'a.py:run' })} />,
    )
    await screen.findByLabelText('Name')
    expect(screen.queryByLabelText('Chat folder')).not.toBeInTheDocument()
  })

  it('refuses input and says why while Hide in chat is on', async () => {
    // hide_in_chat is the explicit "no tab" opt-out, and a run with no tab has
    // nothing to file -- so an enabled picker there would accept a setting whose
    // only observable effect is a folder that never fills up.
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    const picker = await screen.findByLabelText('Chat folder')
    expect(picker).toBeEnabled()

    await userEvent.click(screen.getByLabelText('Hide in chat'))
    expect(await screen.findByText(/Turn off/)).toBeVisible()
    expect(screen.getByLabelText('Chat folder')).toBeDisabled()
  })

  it('sends no placement once Hide in chat is on', async () => {
    const body = buildBody(
      { ...parseJobDefaults(undefined), name: 'n', message: 'm', chatFolderId: 'home', hideInChat: true },
      'UTC',
      () => {},
    )
    expect(body?.chat_folder_id).toBe('')
  })

  it('says the folder list failed rather than showing an empty one', async () => {
    // An empty list is indistinguishable from the real empty tree, so the reader
    // would conclude they need to create a folder they already have.
    vi.mocked(api.chatFolders).mockRejectedValue(new Error('offline'))
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    expect(await screen.findByText(/Could not load your chat folders/)).toBeVisible()
  })
})
