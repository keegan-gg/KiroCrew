import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { pathToFileURL } from 'node:url'

import { expect, test } from '@playwright/test'

const PROJECT_NAME = 'Launchpad'
const UPDATED_PROJECT_NAME = 'Launchpad Workspace'

/** The Project payload as `_project_payload` returns it, narrowed to what this
 *  test reads: the registry-assigned Project id, and each source's synthesized
 *  id beside the URL the manifest declared. */
interface ProjectPayload {
  id: string
  workspace_source: string
  sources: Array<{ id: string; type: string; url?: string }>
}

/** The synthesized id of the source declaring `url`. The manifest cannot name
 *  a source id -- the registry derives it -- so every path and every health
 *  reference in this test is keyed from the payload instead of a literal. */
function sourceIdFor(project: ProjectPayload, url: string): string {
  const source = project.sources.find(candidate => candidate.url === url)
  if (!source?.id) {
    throw new Error(`no source id for ${url} in ${JSON.stringify(project.sources)}`)
  }
  return source.id
}

function runGit(cwd: string, ...args: string[]) {
  execFileSync('git', args, { cwd, stdio: 'pipe' })
}

function createRepository(root: string, name: string, marker: string): string {
  const repository = join(root, name)
  mkdirSync(repository, { recursive: true })
  runGit(root, 'init', '--initial-branch=main', repository)
  runGit(repository, 'config', 'user.name', 'Kiro Crew E2E')
  runGit(repository, 'config', 'user.email', 'e2e@example.invalid')
  writeFileSync(join(repository, 'README.md'), `${marker}\n`, 'utf8')
  runGit(repository, 'add', 'README.md')
  runGit(repository, 'commit', '-m', 'initial fixture')
  return repository
}

test('adds a multi-repo thin Project and starts a usable attached session', async ({ page, request }) => {
  const fixtureRoot = mkdtempSync(join(tmpdir(), 'kirocrew-project-e2e-'))
  // Assigned by the registry when the bundle is added; read back from the
  // add response, since the manifest carries no id.
  let projectId = ''
  const screenshotRoot = process.env.PROJECTS_SCREENSHOT_DIR
  let slotKey = ''
  async function capture(name: string) {
    if (!screenshotRoot) return
    mkdirSync(screenshotRoot, { recursive: true })
    await page.screenshot({ animations: 'disabled', path: join(screenshotRoot, name) })
  }

  async function scrollContentToTop() {
    await page.locator('#main-content').evaluate(element => { element.scrollTop = 0 })
    await page.evaluate(() => window.scrollTo(0, 0))
  }

  try {
    const bundle = join(fixtureRoot, 'bundle')
    mkdirSync(bundle, { recursive: true })
    const webRemote = createRepository(fixtureRoot, 'web-remote', 'launchpad web repository')
    const serviceRemote = createRepository(fixtureRoot, 'service-remote', 'launchpad service repository')
    const docsRemote = createRepository(fixtureRoot, 'docs-remote', 'launchpad docs repository')
    // The thin manifest declares intent only: a name, a description and repos
    // (one primary). No id (the registry assigns one), no `context` block
    // (agents / skills / mcp files reach a session through cwd discovery), and
    // nothing is installed at activation.
    writeFileSync(join(bundle, 'project.yaml'), JSON.stringify({
      apiVersion: 'crew.kiro/v1',
      kind: 'Project',
      name: PROJECT_NAME,
      description: 'A portable two-repository Project used to verify the complete session flow.',
      // No per-source id either: the reduced schema's repo-source keys are
      // exactly {type, url, default_branch, role}, and an `id` is refused by
      // name. The registry synthesizes each source id from its URL, and the
      // checkout directory under `sources/` is keyed by that id -- so every
      // id this test needs is read back from the API payload below.
      sources: [
        { type: 'repo', url: webRemote, role: 'reference' },
        { type: 'repo', url: serviceRemote, role: 'primary' },
      ],
    }, null, 2), 'utf8')
    runGit(fixtureRoot, 'init', '--initial-branch=main', bundle)
    runGit(bundle, 'config', 'user.name', 'Kiro Crew E2E')
    runGit(bundle, 'config', 'user.email', 'e2e@example.invalid')
    runGit(bundle, 'add', '.')
    runGit(bundle, 'commit', '-m', 'project bundle fixture')
    const bundleRemote = pathToFileURL(bundle).href

    await page.setViewportSize({ width: 1440, height: 1100 })
    // Projects is a left-rail page (sibling of Sessions), not a Capabilities tab.
    await page.goto('/project-bundles', { waitUntil: 'domcontentloaded' })
    await expect(
      page.getByRole('navigation', { name: 'Main navigation' }).getByRole('button', { name: 'Projects', exact: true }),
    ).toBeVisible({ timeout: 10000 })

    // Header action and form submit carry the SAME label; the submit is
    // scoped to the form, which is the only one on screen once it is open.
    await page.getByRole('button', { name: 'Add existing project', exact: true }).click()
    await page.getByRole('textbox', { name: 'Folder or Git URL' }).fill(bundleRemote)
    const addResponsePromise = page.waitForResponse(response => (
      response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/project-bundles/add'
    ))
    await page.locator('form').getByRole('button', { name: 'Add existing project', exact: true }).click()
    const addResponse = await addResponsePromise
    if (!addResponse.ok()) {
      throw new Error(
        `Project add request failed (${addResponse.status()}): ${await addResponse.text()}`,
      )
    }

    const added = await addResponse.json() as ProjectPayload
    projectId = added.id
    expect(projectId).not.toBe('')
    // Checkout directory names are the registry's synthesized source ids, not
    // anything the manifest said, so they are looked up by URL from the payload.
    const webSourceId = sourceIdFor(added, webRemote)
    const serviceSourceId = sourceIdFor(added, serviceRemote)
    expect(added.workspace_source).toBe(serviceSourceId)
    const currentProject = page.locator(`[data-project-id="${projectId}"]`)
    await expect(currentProject).toHaveAccessibleName(new RegExp(`^Open project ${PROJECT_NAME}`), { timeout: 10000 })
    await capture('01-project-list.png')

    await currentProject.click()
    await expect(page.getByRole('heading', { name: PROJECT_NAME, exact: true })).toBeVisible()
    await expect(page.getByText(webRemote, { exact: true })).toBeVisible()
    await expect(page.getByText(serviceRemote, { exact: true })).toBeVisible()
    // Nothing the manifest does not carry is listed: no card for MCP or memory.
    await expect(page.getByText('Declared context', { exact: true })).toHaveCount(0)
    await scrollContentToTop()
    await capture('02-project-detail.png')

    writeFileSync(join(bundle, 'project.yaml'), JSON.stringify({
      apiVersion: 'crew.kiro/v1',
      kind: 'Project',
      name: UPDATED_PROJECT_NAME,
      description: 'One focused Project for launch work across code, services, and docs.',
      sources: [
        { type: 'repo', url: webRemote, default_branch: 'main', role: 'reference' },
        { type: 'repo', url: serviceRemote, default_branch: 'main', role: 'primary' },
        { type: 'repo', url: docsRemote, default_branch: 'main', role: 'reference' },
      ],
    }, null, 2), 'utf8')
    runGit(bundle, 'add', 'project.yaml')
    runGit(bundle, 'commit', '-m', 'update project sources')
    const syncResponsePromise = page.waitForResponse(response => (
      response.request().method() === 'POST'
      && new URL(response.url()).pathname === `/api/project-bundles/${projectId}/sync`
    ))
    // One "Sync project" while the Project is healthy: the second copy of that
    // button only appears beside "Review files", i.e. after this sync.
    await page.getByRole('button', { name: 'Sync project', exact: true }).click()
    const syncResponse = await syncResponsePromise
    if (!syncResponse.ok()) {
      throw new Error(`Project sync request failed (${syncResponse.status()}): ${await syncResponse.text()}`)
    }
    const synced = await syncResponse.json() as ProjectPayload
    const docsSourceId = sourceIdFor(synced, docsRemote)
    expect(new Set([webSourceId, serviceSourceId, docsSourceId]).size).toBe(3)

    await expect(page.getByRole('heading', { name: UPDATED_PROJECT_NAME, exact: true })).toBeVisible({ timeout: 10000 })
    await expect(page.getByText(docsRemote, { exact: true })).toBeVisible()

    // The sync deliberately does NOT ratify what it pulled: `project.yaml` is a
    // reviewed surface and it just changed, so the Project comes back
    // review_stale and starting a session is withheld until the owner reads the
    // new bytes in the digest-bound dialog and accepts them.
    await expect(page.getByText('Review needed', { exact: true })).toBeVisible({ timeout: 10000 })
    await expect(page.getByRole('button', { name: 'New session', exact: true })).toBeDisabled()
    await page.getByRole('button', { name: 'Review files', exact: true }).click()
    const reviewDialog = page.getByRole('dialog')
    await expect(reviewDialog.getByTestId('project-review-file').first()).toBeVisible({ timeout: 10000 })
    await expect(reviewDialog.getByText('project.yaml', { exact: true })).toBeVisible()
    await reviewDialog.getByRole('button', { name: 'Accept these changes', exact: true }).click()
    await expect(page.getByText('Healthy', { exact: true })).toBeVisible({ timeout: 10000 })
    await expect(page.getByRole('button', { name: 'New session', exact: true })).toBeEnabled()

    await page.reload({ waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('heading', { name: UPDATED_PROJECT_NAME, exact: true })).toBeVisible({ timeout: 10000 })

    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto(`/project-bundles?project=${projectId}`, { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('heading', { name: UPDATED_PROJECT_NAME, exact: true })).toBeVisible()
    await expect(page.getByText(docsRemote, { exact: true })).toBeVisible()
    await page.setViewportSize({ width: 1440, height: 1100 })

    // No activation step: the thin Project installs nothing, so there is no
    // trust-and-activate control and no capabilities block on the detail JSON.
    await expect(page.getByRole('button', { name: 'Trust and activate', exact: true })).toHaveCount(0)

    await page.goto('/chat', { waitUntil: 'domcontentloaded' })
    await expect(page.getByPlaceholder(/message/i)).toBeVisible({ timeout: 10000 })
    const startingSlotKey = new URL(page.url()).searchParams.get('sid') || ''
    const createMenu = page.locator('[data-create-menu]')
    await expect(createMenu.getByRole('button', { name: 'More create options' })).toBeVisible({ timeout: 10000 })
    await createMenu.getByRole('button', { name: 'More create options' }).click()
    await expect(page.getByRole('menuitem', { name: new RegExp(`^${UPDATED_PROJECT_NAME}`) })).toBeVisible()
    await capture('03-project-new-session-menu.png')
    await page.getByRole('menuitem', { name: new RegExp(`^${UPDATED_PROJECT_NAME}`) }).click()
    await expect.poll(
      () => new URL(page.url()).searchParams.get('sid') || '',
      { timeout: 15000 },
    ).not.toBe(startingSlotKey)
    slotKey = new URL(page.url()).searchParams.get('sid') || ''
    expect(slotKey).not.toBe('')
    await expect(page.getByPlaceholder(/message/i)).toBeVisible({ timeout: 10000 })

    const slotsResponse = await request.get('/api/chat/slots')
    expect(slotsResponse.ok()).toBe(true)
    const slots = await slotsResponse.json() as Array<{ key: string; project?: string; project_id?: string }>
    const slot = slots.find(candidate => candidate.key === slotKey)
    expect(slot?.project_id).toBe(projectId)
    expect(slot?.project).toBeTruthy()
    const workspace = slot?.project as string
    expect(workspace.endsWith(join('sources', serviceSourceId))).toBe(true)
    const web = join(dirname(workspace), webSourceId)
    const docs = join(dirname(workspace), docsSourceId)
    expect(existsSync(join(workspace, 'README.md'))).toBe(true)
    expect(existsSync(join(web, 'README.md'))).toBe(true)
    expect(existsSync(join(docs, 'README.md'))).toBe(true)
    expect(readFileSync(join(docs, 'README.md'), 'utf8')).toContain('launchpad docs repository')

    const messageInput = page.getByPlaceholder(/message/i)
    await messageInput.fill('Confirm this Project session is ready.')
    await page.keyboard.press('Enter')
    await expect(page.locator('.msg-content').getByText('pong from the fake ACP backend', { exact: false }).first()).toBeVisible({ timeout: 15000 })
    // The disabled chip's accessible name carries the reason it is locked, not
    // just the Project name (the same string the tooltip shows).
    await expect(page.getByRole('button', {
      name: `Project: ${UPDATED_PROJECT_NAME}. This session's Project sets the workspace.`,
      exact: true,
    })).toBeVisible()
    await expect(page.getByRole('button', { name: /Copy branch name/ })).toHaveCount(0)
    await capture('04-project-session.png')

    await page.goto('/project-bundles', { waitUntil: 'domcontentloaded' })
    await expect(currentProject).toHaveAccessibleName(new RegExp(`^Open project ${UPDATED_PROJECT_NAME}`), { timeout: 10000 })
    await currentProject.click()
    await expect(page.getByRole('heading', { name: /^Sessions/ })).toBeVisible()
    await expect(page.locator(`a[href="/chat?sid=${slotKey}"]`)).toBeVisible()
    await page.getByRole('button', { name: 'Remove from Kiro Crew', exact: true }).click()
    const removeDialog = page.getByRole('dialog', { name: `Remove ${UPDATED_PROJECT_NAME}?`, exact: true })
    await expect(removeDialog).toBeVisible()
    await expect(removeDialog.getByText('Folders you added stay on disk. Kiro Crew removes only storage it created for this project.', { exact: true })).toBeVisible()
    // The confirm repeats the trigger's own label, so it is scoped to the
    // dialog -- the button that opened it reads the same.
    await removeDialog.getByRole('button', { name: 'Remove from Kiro Crew', exact: true }).click()
    await expect(currentProject).toHaveCount(0)
    await expect(page.getByTestId('project-bundles-empty')).toBeVisible()
    expect(existsSync(join(bundle, 'project.yaml'))).toBe(true)
    await capture('05-project-removed.png')
  } finally {
    if (slotKey) await request.delete(`/api/chat/slots/${encodeURIComponent(slotKey)}`).catch(() => {})
    if (projectId) await request.delete(`/api/project-bundles/${encodeURIComponent(projectId)}`).catch(() => {})
    rmSync(fixtureRoot, { recursive: true, force: true })
  }
})
