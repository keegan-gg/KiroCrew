import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft,
  ChevronRight,
  FolderGit2,
  FolderKanban,
  GitBranch,
  MessageSquare,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
} from 'lucide-react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { api } from '../api/client'
import Clickable from '../components/Clickable'
import { useConfirm } from '../components/ConfirmDialog'
import ErrorNotice from '../components/ErrorNotice'
import ProjectReviewDialog from '../components/ProjectReviewDialog'
import {
  Badge,
  Btn,
  Card,
  CardTitle,
  ContentSkeleton,
  EmptyState,
  Input,
  PageHeader,
  SendBtn,
} from '../components/ui'
import { fmtNumber } from '../i18n/format'
import { i18nT } from '../i18n/t'
import { useAppDispatch } from '../store'
import { createSlot } from '../store/chatSlice'
import type {
  ProjectBundle,
  ProjectBundlesResponse,
} from '../types'
import { parseErrorCode } from '../utils/errorReport'
import { projectHealthBadge as healthBadge } from '../utils/projectHealth'

const PROJECTS_QUERY_KEY = ['project-bundles'] as const

/** The path of the Project's local copy — where `project.yaml` lives — as the
 *  Local copy card shows it. The newest registration is the one on disk here. */
function localCopyPath(project: ProjectBundle): string {
  return project.registrations[project.registrations.length - 1]?.path ?? project.id
}

/** The 'a declared repository could not be cloned' notice, shared by the
 *  `sources_unavailable` state and the case where a missing secondary source
 *  rides along with `review_stale`. Leads with the action, then names the
 *  manifest by its path on its own line (the UI never opens `project.yaml`
 *  itself), and lists the synthesized source ids in monospace below; it does
 *  NOT reuse the manifest-unavailable copy. */
function UnavailableSourcesNotice({ ids, manifestPath }: { ids: string[]; manifestPath: string }) {
  return (
    <>
      <ErrorNotice message={i18nT('pages.projectBundlesPage.sources_unavailable_help', { path: `${manifestPath.replace(/[\\/]+$/, '')}/project.yaml` })} askAgent />
      {ids.length ? (
        <ul className="mt-2 space-y-1">
          {ids.map(id => (
            <li className="break-all font-mono text-[12px] text-muted" key={id}>{id}</li>
          ))}
        </ul>
      ) : null}
    </>
  )
}

function Field({ id, label, children }: { id: string; label: string; children: React.ReactNode }) {
  return (
    <div className="block text-[13px] text-muted">
      <label htmlFor={id}>{label}</label>
      {children}
    </div>
  )
}

function BundleForm({ mode, onClose }: { mode: 'create' | 'add'; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [source, setSource] = useState('')
  const mutation = useMutation({
    mutationFn: () => mode === 'create'
      ? api.createProjectBundle(name.trim(), path.trim())
      : api.addProjectBundle(source.trim()),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY })
      onClose()
    },
  })
  const canSubmit = mode === 'create'
    ? Boolean(name.trim() && path.trim())
    : Boolean(source.trim())

  return (
    <Card className="max-w-4xl">
      <CardTitle>
        {mode === 'create'
          ? i18nT('pages.projectBundlesPage.create_project')
          : i18nT('pages.projectBundlesPage.add_project')}
      </CardTitle>
      <p className="mb-4 text-[13px] text-muted">
        {mode === 'create'
          ? i18nT('pages.projectBundlesPage.create_project_help')
          : i18nT('pages.projectBundlesPage.add_project_help')}
      </p>
      <form className="space-y-3" onSubmit={event => { event.preventDefault(); if (canSubmit && !mutation.isPending) mutation.mutate() }}>
        {mode === 'create' ? (
          <>
            <Field id="project-bundle-name" label={i18nT('pages.projectBundlesPage.project_name')}>
              <Input id="project-bundle-name" name="project-name" autoComplete="off" className="mt-2 w-full" value={name} onChange={event => setName(event.target.value)} />
            </Field>
            <Field id="project-bundle-path" label={i18nT('pages.projectBundlesPage.bundle_folder')}>
              <Input id="project-bundle-path" name="bundle-path" autoComplete="off" className="mt-2 w-full font-mono" value={path} onChange={event => setPath(event.target.value)} />
            </Field>
          </>
        ) : (
          <Field id="project-bundle-source" label={i18nT('pages.projectBundlesPage.folder_or_git_url')}>
            <Input id="project-bundle-source" name="project-source" autoComplete="url" className="mt-2 w-full font-mono" value={source} onChange={event => setSource(event.target.value)} />
          </Field>
        )}
        {/* No hand-off: the create/add form's name, folder and source fields are
            unsaved drafts — navigating to the chat would discard them. */}
        <RequestError error={mutation.error} />
        <div className="flex flex-wrap gap-2">
          <SendBtn className="inline-flex items-center gap-1.5" type="submit" disabled={!canSubmit || mutation.isPending}>
            {mode === 'create'
              ? i18nT('pages.projectBundlesPage.create_project')
              : i18nT('pages.projectBundlesPage.add_project')}
          </SendBtn>
          <Btn type="button" onClick={onClose}>{i18nT('pages.projectBundlesPage.cancel')}</Btn>
        </div>
      </form>
    </Card>
  )
}

/** The backend's machine-readable `code` on a failed request, when it carried
 *  one. Duck-typed on `body` (like `isReviewMovedError`) so it holds under a
 *  mocked `api/client`. */
function requestErrorCode(error: unknown): string | undefined {
  if (typeof error !== 'object' || error === null) return undefined
  const { body } = error as { body?: unknown }
  return typeof body === 'string' ? parseErrorCode(body) : undefined
}

/** A host that denies user namespaces cannot run the sandbox every Project
 *  Git operation goes through, so `add` and `sync` are refused with this code.
 *  There is no fallback to offer; the notice repeats the requirement. */
const SANDBOX_UNAVAILABLE = 'project_sandbox_unavailable'

function requestErrorMessage(error: unknown): string | null {
  if (!error) return null
  if (requestErrorCode(error) === SANDBOX_UNAVAILABLE) return i18nT('pages.projectBundlesPage.sandbox_unavailable_help')
  return error instanceof Error && error.message
    ? error.message
    : i18nT('pages.projectBundlesPage.project_request_failed')
}

/**
 * A failed Project request, rendered through `ErrorNotice` so the structured
 * context (endpoint, status, backend code) survives. `askAgent` is decided by
 * the call site: on where the surface holds nothing unsaved, off beside a draft.
 * A sandbox refusal is the one exception: it is a host requirement the form
 * cannot satisfy, so the hand-off is offered there too — the draft it would
 * discard is a single path or URL the owner can retype.
 */
function RequestError({ error, askAgent = false, className }: { error: unknown; askAgent?: boolean; className?: string }) {
  const sandbox = requestErrorCode(error) === SANDBOX_UNAVAILABLE
  return <ErrorNotice message={requestErrorMessage(error)} askAgent={askAgent || sandbox} className={className} />
}

function ProjectDetails({ project, onBack }: { project: ProjectBundle; onBack: () => void }) {
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const [synced, setSynced] = useState(false)
  const repositories = project.sources.filter(source => source.type === 'repo')
  const { confirm, confirmDialog } = useConfirm()
  const sessionMutation = useMutation({
    mutationFn: () => dispatch(createSlot({ project_id: project.id })).unwrap(),
    onSuccess: slot => navigate(`/chat?sid=${encodeURIComponent(slot.key)}`),
  })
  const syncMutation = useMutation({
    mutationFn: () => api.syncProjectBundle(project.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY })
      setSynced(true)
    },
  })
  const removeMutation = useMutation({
    mutationFn: () => api.removeProjectBundle(project.id),
    onSuccess: async () => {
      onBack()
      await queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY })
    },
  })
  const [reviewOpen, setReviewOpen] = useState(false)
  const syncable = project.registrations.some(registration => registration.syncable)

  async function removeProject() {
    // The confirm repeats the trigger's own label: the button that opened
    // this dialog reads "Remove from …", and the action that completes it
    // reads the same, so the owner confirms the thing they clicked.
    const accepted = await confirm({
      title: i18nT('pages.projectBundlesPage.remove_project_title', { name: project.name }),
      body: i18nT('pages.projectBundlesPage.remove_project_body'),
      confirmLabel: i18nT('pages.projectBundlesPage.remove_from_kiro_crew'),
    })
    if (accepted) removeMutation.mutate()
  }

  return (
    <div className="max-w-4xl">
      <Btn className="mb-3" onClick={onBack}>
        <ArrowLeft className="lucide-inline" />
        {i18nT('pages.projectBundlesPage.back_to_projects')}
      </Btn>
      <Card>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <h2 className="text-xl font-semibold text-text-strong">{project.name}</h2>
              <Badge variant={healthBadge(project.health.status).variant}>
                {healthBadge(project.health.status).label}
              </Badge>
            </div>
            <p className="max-w-2xl text-sm text-muted">{project.description || i18nT('pages.projectBundlesPage.no_description')}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <SendBtn className="inline-flex items-center gap-1.5" disabled={sessionMutation.isPending || project.health.status !== 'healthy'} onClick={() => sessionMutation.mutate()}>
              <MessageSquare className="lucide-inline" />
              {i18nT('pages.projectBundlesPage.new_session')}
            </SendBtn>
          </div>
        </div>
        {project.health.status === 'review_stale' ? (
          <div className="mt-4">
            <ErrorNotice message={i18nT('pages.projectBundlesPage.review_stale_help')} askAgent />
            {project.health.stale_files?.length ? (
              <ul className="mt-2 space-y-1">
                {project.health.stale_files.map(file => (
                  <li className="break-all font-mono text-[12px] text-muted" key={file}>{file}</li>
                ))}
              </ul>
            ) : null}
            <div className="mt-2 flex flex-wrap gap-2">
              {/* Opens the digest-bound review: the files' content is shown
                  and accepted in the dialog, never from this button alone. */}
              <Btn onClick={() => setReviewOpen(true)}>
                <ShieldCheck className="lucide-inline" />
                {i18nT('pages.projectBundlesPage.review_changes')}
              </Btn>
              {/* Every unreadable entry's remedy ends in "sync", so the sync
                  action stands beside the review it feeds. */}
              {syncable && (
                <Btn disabled={syncMutation.isPending} onClick={() => { setSynced(false); syncMutation.mutate() }}>
                  <RefreshCw className="lucide-inline" />
                  {i18nT('pages.projectBundlesPage.sync_project')}
                </Btn>
              )}
            </div>
            {/* A stale digest outranks a missing SECONDARY source, so the two
                states co-occur — show the sources notice as well. */}
            {project.health.unavailable_sources?.length ? (
              <div className="mt-4">
                <UnavailableSourcesNotice ids={project.health.unavailable_sources} manifestPath={localCopyPath(project)} />
              </div>
            ) : null}
          </div>
        ) : project.health.status === 'sources_unavailable' ? (
          <div className="mt-4">
            <UnavailableSourcesNotice ids={project.health.unavailable_sources ?? []} manifestPath={localCopyPath(project)} />
            {syncable && (
              <div className="mt-2 flex flex-wrap gap-2">
                <Btn disabled={syncMutation.isPending} onClick={() => { setSynced(false); syncMutation.mutate() }}>
                  <RefreshCw className="lucide-inline" />
                  {i18nT('pages.projectBundlesPage.sync_project')}
                </Btn>
              </div>
            )}
          </div>
        ) : project.health.status !== 'healthy' ? (
          <div className="mt-4">
            <ErrorNotice message={i18nT('pages.projectBundlesPage.manifest_unavailable_help')} askAgent />
            {syncable && (
              <div className="mt-2 flex flex-wrap gap-2">
                <Btn disabled={syncMutation.isPending} onClick={() => { setSynced(false); syncMutation.mutate() }}>
                  <RefreshCw className="lucide-inline" />
                  {i18nT('pages.projectBundlesPage.retry_sync')}
                </Btn>
              </div>
            )}
          </div>
        ) : null}
        <RequestError error={sessionMutation.error} askAgent className="mt-4" />
      </Card>

      <Card>
        <CardTitle>
          {i18nT('pages.projectBundlesPage.sessions')}
          <Badge variant="muted">{fmtNumber(project.sessions?.length ?? 0)}</Badge>
        </CardTitle>
        {project.sessions?.length ? (
          <div className="space-y-2">
            {project.sessions.map(session => (
              <Link className="flex min-w-0 items-center justify-between gap-3 rounded-md border border-border bg-bg-elevated px-3 py-2 transition-colors hover:border-accent hover:bg-bg-hover" key={session.key} to={`/chat?sid=${encodeURIComponent(session.key)}`}>
                <span className="truncate text-sm text-text">{session.title}</span>
                <span className="shrink-0 text-[12px] text-muted">{i18nT('pages.projectBundlesPage.message_count', { count: session.messages })}</span>
              </Link>
            ))}
          </div>
        ) : <div className="text-[13px] text-muted">{i18nT('pages.projectBundlesPage.no_sessions')}</div>}
      </Card>

      <Card>
        <CardTitle>{i18nT('pages.projectBundlesPage.overview')}</CardTitle>
        <div className="space-y-3 text-[13px]">
          <div>
            <div className="text-muted">{i18nT('pages.projectBundlesPage.working_repository')}</div>
            <div className="mt-1 font-mono text-text">{project.workspace_source === 'self' ? i18nT('pages.projectBundlesPage.project_bundle') : project.workspace_source}</div>
          </div>
        </div>
      </Card>

      <Card>
        <CardTitle>
          <GitBranch className="lucide-inline" />
          {i18nT('pages.projectBundlesPage.repositories')}
          <Badge variant="muted">{fmtNumber(repositories.length)}</Badge>
        </CardTitle>
        {repositories.length ? (
          <div className="space-y-2">
            {repositories.map(source => (
              <div className="rounded-md border border-border bg-bg-elevated px-3 py-3" key={source.id}>
                <div className="flex flex-wrap items-center gap-2 font-medium text-text">
                  {source.id}
                  {/* The health banner names the failing ids; the row they
                      describe must not read as healthy beside it. The badge
                      names what happened (the clone failed), not a state. */}
                  {project.health.unavailable_sources?.includes(source.id) && (
                    <Badge variant="err">{i18nT('pages.projectBundlesPage.clone_failed')}</Badge>
                  )}
                </div>
                {typeof source.url === 'string' && <div className="mt-1 break-all font-mono text-[13px] text-muted">{source.url}</div>}
                {typeof source.default_branch === 'string' && source.default_branch && <div className="mt-1 text-[13px] text-muted">{i18nT('pages.projectBundlesPage.default_branch')}: <span className="font-mono text-text">{source.default_branch}</span></div>}
              </div>
            ))}
          </div>
        ) : <div className="text-[13px] text-muted">{i18nT('pages.projectBundlesPage.no_sources')}</div>}
      </Card>

      <Card>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <CardTitle className="mb-0">{i18nT('pages.projectBundlesPage.local_copy')}</CardTitle>
          {syncable && (
            <Btn disabled={syncMutation.isPending} onClick={() => { setSynced(false); syncMutation.mutate() }}>
              <RefreshCw className="lucide-inline" />
              {i18nT('pages.projectBundlesPage.sync_project')}
            </Btn>
          )}
        </div>
        <div className="mt-3 space-y-2">
          <div>
            <div className="text-[12px] text-muted">{i18nT('pages.projectBundlesPage.project_id')}</div>
            <div className="mt-1 break-all font-mono text-[13px] text-text">{project.id}</div>
          </div>
          {project.registrations.map(registration => (
            <div className="rounded-md border border-border bg-bg-elevated px-3 py-2" key={`${registration.origin}:${registration.path}`}>
              <div className="text-[12px] text-muted">
                {registration.syncable
                  ? i18nT('pages.projectBundlesPage.shared_with_git')
                  : i18nT('pages.projectBundlesPage.local_project')}
              </div>
              <div className="mt-1 break-all font-mono text-[13px] text-text">{registration.path}</div>
            </div>
          ))}
        </div>
        {synced && <div className="mt-3 text-[13px] text-ok" role="status" aria-live="polite">{i18nT('pages.projectBundlesPage.project_synced')}</div>}
        <RequestError error={syncMutation.error} askAgent className="mt-3" />
        <div className="mt-4 border-t border-border pt-4">
          <Btn danger disabled={removeMutation.isPending} onClick={() => { void removeProject() }}>
            <Trash2 className="lucide-inline" />
            {i18nT('pages.projectBundlesPage.remove_from_kiro_crew')}
          </Btn>
          <RequestError error={removeMutation.error} askAgent className="mt-3" />
        </div>
      </Card>
      {confirmDialog}
      <ProjectReviewDialog project={project} open={reviewOpen} onClose={() => setReviewOpen(false)} />
    </div>
  )
}

function ProjectList({ projects, onOpen }: { projects: ProjectBundle[]; onOpen: (id: string) => void }) {
  return (
    <div className="max-w-4xl space-y-3">
      {projects.map(project => (
        <Clickable
          aria-label={`${i18nT('pages.projectBundlesPage.open_project', { name: project.name })} — ${project.registrations[project.registrations.length - 1]?.path ?? project.id}`}
          className="group flex min-w-0 items-center justify-between gap-4 rounded-lg border border-border bg-card px-4 py-4 shadow-sm transition-colors hover:border-accent hover:bg-bg-hover"
          data-project-id={project.id}
          key={project.id}
          onClick={() => onOpen(project.id)}
        >
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <div className="truncate text-base font-semibold text-text-strong">{project.name}</div>
              <Badge variant={healthBadge(project.health.status).variant}>
                {healthBadge(project.health.status).label}
              </Badge>
            </div>
            <div className="mt-1 line-clamp-2 text-[13px] text-muted">{project.description || i18nT('pages.projectBundlesPage.no_description')}</div>
            <div className="mt-1 truncate font-mono text-[12px] text-muted">{project.registrations[project.registrations.length - 1]?.path ?? project.id}</div>
            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-muted">
              <span className="inline-flex items-center gap-1.5">
                {i18nT('pages.projectBundlesPage.repositories')}
                <Badge variant="muted">{fmtNumber(project.sources.filter(source => source.type === 'repo').length)}</Badge>
              </span>
              <span className="inline-flex items-center gap-1.5">
                {i18nT('pages.projectBundlesPage.sessions')}
                <Badge variant="muted">{fmtNumber(project.sessions?.length ?? 0)}</Badge>
              </span>
              <span>{project.registrations.some(registration => registration.origin === 'managed_git') ? i18nT('pages.projectBundlesPage.shared_with_git') : i18nT('pages.projectBundlesPage.local_project')}</span>
            </div>
          </div>
          <ChevronRight className="lucide-inline shrink-0 text-muted transition-colors group-hover:text-text" />
        </Clickable>
      ))}
    </div>
  )
}

function ProjectBundlesContent({ embedded }: { embedded: boolean }) {
  const [params, setParams] = useSearchParams()
  const selectedId = params.get('project')
  const view = params.get('view')
  const form = view === 'create' || view === 'add' ? view : null
  const projectsQuery = useQuery<ProjectBundlesResponse>({
    queryKey: PROJECTS_QUERY_KEY,
    queryFn: () => api.projectBundles(),
  })
  const projects = projectsQuery.data?.projects ?? []
  const selected = selectedId ? projects.find(project => project.id === selectedId) : undefined
  const updateRoute = (changes: { project?: string | null; view?: string | null }, replace = false) => {
    setParams(current => {
      const next = new URLSearchParams(current)
      for (const [key, value] of Object.entries(changes)) {
        if (value) next.set(key, value)
        else next.delete(key)
      }
      return next
    }, { replace })
  }
  useEffect(() => {
    if (projectsQuery.isLoading || !selectedId || selected) return
    setParams(current => {
      const next = new URLSearchParams(current)
      next.delete('project')
      next.delete('view')
      return next
    }, { replace: true })
  }, [projectsQuery.isLoading, selected, selectedId, setParams])
  const actions = !selected ? (
    <>
      <Btn onClick={() => updateRoute({ project: null, view: 'add' })}>
        <FolderGit2 className="lucide-inline" />
        {i18nT('pages.projectBundlesPage.add_project')}
      </Btn>
      <SendBtn className="inline-flex items-center gap-1.5" onClick={() => updateRoute({ project: null, view: 'create' })}>
        <Plus className="lucide-inline" />
        {i18nT('pages.projectBundlesPage.create_project')}
      </SendBtn>
    </>
  ) : null

  return (
    <>
      {!embedded && <PageHeader title={i18nT('pages.projectBundlesPage.projects')} subtitle={i18nT('pages.projectBundlesPage.subtitle')} actions={actions} />}
      <div className={embedded ? 'pb-8' : 'px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0'}>
        {embedded && actions && <div className="mb-3 flex flex-wrap justify-end gap-2">{actions}</div>}
        {form && !selected && <BundleForm mode={form} onClose={() => updateRoute({ view: null })} />}
        {projectsQuery.isLoading ? (
          <Card className="max-w-4xl"><ContentSkeleton rows={4} /></Card>
        ) : projectsQuery.error ? (
          <Card className="max-w-4xl"><ErrorNotice message={i18nT('pages.projectBundlesPage.failed_to_load_projects')} askAgent /></Card>
        ) : projects.length === 0 ? (
          <Card className="max-w-4xl">
            <EmptyState icon={<FolderKanban className="lucide-inline" />} title={i18nT('pages.projectBundlesPage.no_projects_yet')} subtitle={i18nT('pages.projectBundlesPage.empty_subtitle')} testId="project-bundles-empty" />
          </Card>
        ) : selected ? (
          <ProjectDetails
            key={selected.id}
            project={selected}
            onBack={() => updateRoute({ project: null, view: null })}
          />
        ) : (
          <ProjectList projects={projects} onOpen={id => updateRoute({ project: id, view: null })} />
        )}
      </div>
    </>
  )
}

export default function ProjectBundlesPage({ embedded = false }: { embedded?: boolean }) {
  return <ProjectBundlesContent embedded={embedded} />
}
