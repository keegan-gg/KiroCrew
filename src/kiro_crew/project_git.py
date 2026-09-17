"""Managed Git transport for portable Project bundles."""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import tempfile
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import unquote, urlsplit

from kiro_crew import platform_compat, sandbox
from kiro_crew.platform.update_governance import git_command_env, repo_exec_config_reason
from kiro_crew.project_manifest import (
    PROJECT_MANIFEST_MAX_BYTES,
    ProjectManifest,
    ProjectManifestError,
    load_project_manifest,
    load_project_manifest_text,
)
from kiro_crew.project_registry import ProjectRegistration, ProjectRegistry, RegisteredProject
from kiro_crew.sandbox import SandboxUnavailableError, run_limited, sandboxed_spawn_argv
from kiro_crew.security import is_sensitive_path

_GIT_TIMEOUT_SECONDS = 120
_SUPPORTED_REMOTE_SCHEMES = frozenset({"file", "git", "http", "https", "ssh"})
_BRANCH_RE = re.compile(r"^(?![-./])(?!.*(?:\.\.|//|@\{|\\|[~^:?*\[]))[^\x00-\x20\x7f]+(?<![./])$")
_KEYCHAIN_HELPERS = frozenset({"libsecret", "manager", "manager-core", "osxkeychain", "wincred"})
_MAX_CREDENTIAL_HELPERS = 8
_ORIGIN_HELPER_CONFIG = {
    "remote.origin.uploadpack": "git-upload-pack",
    "remote.origin.receivepack": "git-receive-pack",
}


class ProjectGitError(ValueError):
    """A managed Project clone could not be created or synchronized safely."""


class ProjectSandboxUnavailableError(ProjectGitError):
    """Project Git operations require an enforcing host sandbox backend."""

    code = "project_sandbox_unavailable"

    def __init__(self) -> None:
        message = (
            "Project git operations run inside the Kiro Crew sandbox, and this host "
            "has no sandbox backend it can enforce."
        )
        reason = sandbox.unavailable_reason()
        super().__init__(f"{message} {reason}" if reason else message)


def _require_project_sandbox() -> None:
    if not sandbox.enforcing_backend_available():
        raise ProjectSandboxUnavailableError()


class GitProjectStore:
    """Own managed Project clones while leaving external checkouts untouched."""

    def __init__(self, registry: ProjectRegistry | None = None) -> None:
        self.registry = registry or ProjectRegistry()

    @staticmethod
    def _git_executable() -> str:
        executable = platform_compat.trusted_git_bin()
        if executable is None:
            raise ProjectGitError("a trusted Git executable is unavailable")
        return executable

    @staticmethod
    def _validate_remote(remote: str, *, base_dir: Path | None = None) -> str:
        remote = remote.strip()
        if not remote:
            raise ProjectGitError("Git Project remote must not be empty")
        if any(character in remote for character in ("\x00", "\r", "\n")):
            raise ProjectGitError("Git Project remote contains invalid characters")
        try:
            parsed = urlsplit(remote)
        except ValueError as exc:
            raise ProjectGitError("invalid Git remote URL") from exc
        scheme = parsed.scheme.lower()
        scp_style = bool(
            re.fullmatch(r"(?![A-Za-z]:[/\\])(?:[^/@:\s]+@)?[^/:\s]+:[^:\s].*", remote)
        )
        if parsed.scheme and not scp_style and scheme not in _SUPPORTED_REMOTE_SCHEMES:
            raise ProjectGitError("unsupported Git remote protocol")
        if scheme in {"http", "https"} and (
            parsed.username is not None or parsed.query or parsed.fragment
        ):
            raise ProjectGitError(
                "HTTP Git remotes must use a credential helper, not credentials in the URL"
            )
        if parsed.password is not None:
            raise ProjectGitError("Git remotes must not include a password")
        local_path: str | None = None
        if scheme == "file":
            if parsed.query or parsed.fragment:
                raise ProjectGitError("file Git remotes must not include a query or fragment")
            if parsed.netloc and parsed.netloc.lower() != "localhost":
                raise ProjectGitError("file Git remotes must be local")
            local_path = unquote(parsed.path)
            if any(character in local_path for character in ("\x00", "\r", "\n")):
                raise ProjectGitError("Git Project remote contains invalid characters")
            if not Path(local_path).is_absolute():
                raise ProjectGitError("file Git remotes must use an absolute path")
        elif not parsed.scheme and not scp_style:
            local_path = remote
        if local_path is not None:
            local = Path(local_path).expanduser()
            if not local.is_absolute():
                local = (base_dir or Path.cwd()) / local
            normalized = str(local.resolve(strict=False))
            if is_sensitive_path(normalized):
                raise ProjectGitError("Git Project remote is a sensitive path")
            if scheme != "file":
                return normalized
        return remote

    @staticmethod
    def _validate_branch(default_branch: str) -> str:
        branch = default_branch.strip()
        if branch and not _BRANCH_RE.fullmatch(branch):
            raise ProjectGitError("Project repo default branch is invalid")
        return branch

    @contextmanager
    def _lock(self, name: str) -> Iterator[None]:
        locks_dir = self.registry.projects_dir / "state" / "git-locks"
        self._assert_derived_path_unlinked(locks_dir)
        platform_compat.make_owner_only_dir(locks_dir)
        lock_path = locks_dir / f"{name}.lock"
        self._assert_derived_path_unlinked(lock_path)
        try:
            fd = os.open(
                str(lock_path),
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as exc:
            raise ProjectGitError("Project Git lock path is not safe") from exc
        try:
            with platform_compat.file_lock(fd, exclusive=True, required=True):
                yield
        finally:
            os.close(fd)

    @staticmethod
    def _sanitize_credential_helper(value: str) -> str | None:
        value = value.strip()
        if value in _KEYCHAIN_HELPERS:
            trusted_helper = platform_compat.trusted_system_bin(f"git-credential-{value}")
            return f"!{shlex.quote(trusted_helper)}" if trusted_helper else None
        if not value.startswith("!"):
            return None
        try:
            argv = shlex.split(value[1:])
        except ValueError:
            return None
        if len(argv) != 3 or Path(argv[0]).name != "gh":
            return None
        if argv[1:] != ["auth", "git-credential"]:
            return None
        trusted_gh = platform_compat.trusted_system_bin("gh")
        return f"!{shlex.quote(trusted_gh)} auth git-credential" if trusted_gh else None

    @classmethod
    def _credential_helper_env(cls) -> dict[str, str]:
        _require_project_sandbox()
        env = git_command_env()
        # Keep missing executable pins fail-closed to the null device; resolve
        # available transport helpers only through fixed trusted system roots.
        for index in range(int(env["GIT_CONFIG_COUNT"])):
            key = env.get(f"GIT_CONFIG_KEY_{index}", "").lower()
            helper_name = _ORIGIN_HELPER_CONFIG.get(key)
            if helper_name is None:
                continue
            helper = platform_compat.trusted_system_bin(helper_name)
            if helper is not None:
                # Git appends the repository path to this shell command, so
                # preserve the helper's absolute path as a single argument.
                env[f"GIT_CONFIG_VALUE_{index}"] = shlex.quote(helper)
        helpers: list[tuple[str, str]] = []
        for scope in ("--system", "--global"):
            cleanup: str | None = None
            try:
                argv, scrubbed, cleanup = sandboxed_spawn_argv(
                    [
                        cls._git_executable(),
                        "config",
                        scope,
                        "--get-regexp",
                        r"^credential(\..+)?\.helper$",
                    ],
                    mode="standard",
                    env=env,
                )
                result = run_limited(
                    argv,
                    env=scrubbed,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=_GIT_TIMEOUT_SECONDS,
                )
            finally:
                if cleanup:
                    Path(cleanup).unlink(missing_ok=True)
            if result.returncode != 0:
                continue
            for line in result.stdout.splitlines():
                key, separator, raw_value = line.partition(" ")
                helper = cls._sanitize_credential_helper(raw_value) if separator else None
                if helper is not None:
                    helpers.append((key, helper))
                if len(helpers) >= _MAX_CREDENTIAL_HELPERS:
                    break
            if len(helpers) >= _MAX_CREDENTIAL_HELPERS:
                break
        start = int(env["GIT_CONFIG_COUNT"])
        for offset, (key, value) in enumerate(helpers):
            env[f"GIT_CONFIG_KEY_{start + offset}"] = key
            env[f"GIT_CONFIG_VALUE_{start + offset}"] = value
        env["GIT_CONFIG_COUNT"] = str(start + len(helpers))
        return env

    @staticmethod
    def _assert_safe_checkout(path: Path) -> None:
        reason = repo_exec_config_reason(str(path))
        if reason:
            raise ProjectGitError(f"Project repository is unsafe to synchronize: {reason}")

    def _assert_derived_path_unlinked(self, path: Path) -> None:
        """Reject links in an install-local Project path without resolving through them."""
        root = self.registry.projects_dir.absolute()
        candidate = path.absolute()
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise ProjectGitError("Project derived path escapes managed storage") from exc
        current = root
        if platform_compat.is_link_or_junction(current):
            raise ProjectGitError("Project derived path contains a link or junction")
        for part in relative.parts:
            current = current / part
            if platform_compat.is_link_or_junction(current):
                raise ProjectGitError("Project derived path contains a link or junction")

    @classmethod
    def _run_git(cls, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        _require_project_sandbox()
        cleanup: str | None = None
        try:
            argv, env, cleanup = sandboxed_spawn_argv(
                [cls._git_executable(), *args],
                mode="standard",
                env=cls._credential_helper_env(),
            )
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["GIT_PROXY_COMMAND"] = "true"
            return run_limited(
                argv,
                cwd=cwd,
                env=env,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except SandboxUnavailableError as exc:
            raise ProjectSandboxUnavailableError() from exc
        except subprocess.TimeoutExpired as exc:
            raise ProjectGitError("Git project operation timed out") from exc
        except subprocess.CalledProcessError as exc:
            # Git commonly echoes a remote URL in stderr. That URL may carry an
            # embedded credential, so the CLI reports the failure without
            # replaying subprocess output.
            raise ProjectGitError("Git project operation failed") from exc
        finally:
            if cleanup:
                Path(cleanup).unlink(missing_ok=True)

    def add(self, remote: str) -> RegisteredProject:
        """Clone a Git-backed bundle into managed storage and register it."""
        remote = self._validate_remote(remote)
        with self._lock("bundle-add"):
            managed_root = self.registry.projects_dir / "managed"
            self._assert_derived_path_unlinked(managed_root)
            managed_root.mkdir(parents=True, exist_ok=True)
            self._assert_derived_path_unlinked(managed_root)
            staging = Path(tempfile.mkdtemp(prefix="project-clone-", dir=managed_root))
            published = False
            try:
                self._assert_derived_path_unlinked(staging)
                self._run_git(managed_root, "clone", "--", remote, str(staging))
                self._assert_derived_path_unlinked(staging)
                self._assert_safe_checkout(staging)
                manifest = load_project_manifest(staging)
                # Pin the branch NOW, from the staging clone this call just made
                # and nothing else has reached: after publish the checkout is
                # agent-writable derived state and its HEAD is never consulted
                # again. `sync` fetches exactly this remote and branch.
                default_branch = self._validate_branch(
                    self._run_git(
                        staging, "symbolic-ref", "--quiet", "--short", "HEAD"
                    ).stdout.strip()
                )
                if not default_branch:
                    raise ProjectGitError("Project bundle clone has a detached HEAD")

                def publish(project_id: str) -> ProjectRegistration:
                    nonlocal published
                    target = managed_root / project_id / "bundle"
                    self._assert_derived_path_unlinked(target)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    self._assert_derived_path_unlinked(target)
                    if target.exists():
                        raise ProjectGitError(f"managed Project path collision at {target}")
                    os.replace(staging, target)
                    published = True
                    return ProjectRegistration(
                        path=target.resolve(),
                        origin="managed_git",
                        remote=remote,
                        default_branch=default_branch,
                    )

                registered = self.registry.add_managed(
                    staging,
                    remote=remote,
                    default_branch=default_branch,
                    publish=publish,
                )
                target = registered.registrations[-1].path
                self._assert_derived_path_unlinked(target)
                self._assert_safe_checkout(target)
                manifest = load_project_manifest(target)
                # Registered first, then materialized: a source that cannot be
                # cloned is reported through Project health, and the owner needs
                # the registration to exist before they can correct the URL and
                # sync. The declared sources are what a session actually runs in
                # when the manifest names a primary.
                self.materialize_sources(registered.id, target, manifest)
                return registered
            except ProjectManifestError as exc:
                raise ProjectGitError(
                    f"Git repository is not a valid Project bundle: {exc}"
                ) from exc
            finally:
                if not published and staging.exists():
                    platform_compat.rmtree_force(staging)

    def sync_source(
        self,
        project_id: str,
        source_id: str,
        remote: str,
        default_branch: str = "",
        *,
        base_dir: Path | None = None,
    ) -> Path:
        """Fast-forward a matching source checkout, or (re)clone it.

        Materializing alone would only ever CREATE a checkout: a tree whose
        provenance still matches its declaration is reused untouched, so a commit
        pushed to a declared repository would never reach the directory a session
        runs in -- and the review digest would never see the ``mcp.json`` that
        commit added. Syncing is therefore a fetch of the PINNED remote and
        branch followed by a fast-forward, exactly as the bundle sync does, with
        everything downstream reading ``FETCH_HEAD``. A checkout whose provenance
        does not match its declaration falls through to a fresh clone, which keeps
        the previous tree until its replacement is recorded.
        """
        remote = self._validate_remote(remote, base_dir=base_dir)
        default_branch = self._validate_branch(default_branch)
        sources_root = self.registry.projects_dir / "state" / project_id / "sources"
        target = sources_root / source_id
        self._assert_derived_path_unlinked(target)
        with self._lock(f"{project_id}-{source_id}"):
            self._assert_derived_path_unlinked(target)
            reusable = (
                target.exists()
                and (target / ".git").exists()
                and self._checkout_matches(sources_root, source_id, remote, default_branch)
            )
            if reusable:
                self._assert_safe_checkout(target)
                fetch_args = (remote, default_branch) if default_branch else (remote,)
                self._run_git(target, "fetch", "--", *fetch_args)
                self._run_git(target, "merge", "--ff-only", "FETCH_HEAD")
                return target
        # Outside the lock: materialize_source takes the same one to publish its
        # replacement, and a checkout that is not reusable is its business.
        return self.materialize_source(
            project_id, source_id, remote, default_branch, base_dir=base_dir
        )

    def sync_sources(
        self, project_id: str, bundle_dir: Path, manifest: ProjectManifest
    ) -> dict[str, str]:
        """Refresh every repo source, reporting failures except sandbox unavailability."""
        return self._for_each_repo_source(project_id, bundle_dir, manifest, self.sync_source)

    def materialize_sources(
        self, project_id: str, bundle_dir: Path, manifest: ProjectManifest
    ) -> dict[str, str]:
        """Clone or reuse every ``type: repo`` source the manifest declares.

        A Project is "a Git repository the gateway materializes and keeps in
        sync", so the declared sources are cloned when the owner adds the Project
        and refreshed when they sync it. Coordinates come from the manifest
        (``url``, ``default_branch``); ``materialize_source`` decides reuse
        against the checkout's provenance record and replaces before discarding,
        so a declaration that moved to another remote re-clones without costing
        the checkout that was working.

        Returns ``{source id: reason}`` for the sources that could not be
        materialized. Unreachable URLs leave the Project registered so the owner
        can correct the manifest and sync; sandbox unavailability is raised.
        """
        return self._for_each_repo_source(project_id, bundle_dir, manifest, self.materialize_source)

    def _for_each_repo_source(
        self,
        project_id: str,
        bundle_dir: Path,
        manifest: ProjectManifest,
        action: Callable[..., Path],
    ) -> dict[str, str]:
        failures: dict[str, str] = {}
        for source in manifest.sources:
            if source.type != "repo":
                continue
            url = source.config.get("url")
            if not isinstance(url, str) or not url.strip():
                failures[source.id] = "source declares no URL"
                continue
            branch = source.config.get("default_branch", "")
            try:
                action(
                    project_id,
                    source.id,
                    url,
                    branch.strip() if isinstance(branch, str) else "",
                    base_dir=bundle_dir,
                )
            except ProjectSandboxUnavailableError:
                raise
            except (ProjectGitError, ProjectManifestError, OSError, RuntimeError) as exc:
                failures[source.id] = str(exc)
        return failures

    def materialize_registered_sources(self, project: RegisteredProject) -> dict[str, str]:
        """Materialize the declared sources of an already-registered Project."""
        bundle_dir = project.registrations[-1].path
        try:
            manifest = load_project_manifest(bundle_dir)
        except ProjectManifestError as exc:
            raise ProjectGitError(f"Project bundle manifest is invalid: {exc}") from exc
        return self.materialize_sources(project.id, bundle_dir, manifest)

    def materialize_source(
        self,
        project_id: str,
        source_id: str,
        remote: str,
        default_branch: str = "",
        *,
        base_dir: Path | None = None,
    ) -> Path:
        """Clone one repo source or reuse its install-local derived checkout."""
        if not remote.strip():
            raise ProjectGitError(f"Project repo source {source_id} needs a URL")
        remote = self._validate_remote(remote, base_dir=base_dir)
        default_branch = self._validate_branch(default_branch)
        sources_root = self.registry.projects_dir / "state" / project_id / "sources"
        target = sources_root / source_id
        self._assert_derived_path_unlinked(target)
        sources_root.mkdir(parents=True, exist_ok=True)
        self._assert_derived_path_unlinked(target)
        with self._lock(f"{project_id}-{source_id}"):
            self._assert_derived_path_unlinked(target)
            if target.exists():
                if not (target / ".git").exists():
                    raise ProjectGitError(
                        f"Project repo source path is not a Git checkout: {source_id}"
                    )
                self._assert_safe_checkout(target)
                # The source id is manifest-declared and independent of the URL
                # and branch, so a declaration that moved to another remote or
                # branch must not keep serving the old checkout under the same
                # id. A mismatch falls through to a fresh clone, which replaces
                # the stale tree below; only a matching checkout is reused.
                if self._checkout_matches(sources_root, source_id, remote, default_branch):
                    return target

            staging = Path(tempfile.mkdtemp(prefix=f".{source_id}-", dir=sources_root))
            published = False
            try:
                branch_args = (
                    ("--branch", default_branch, "--single-branch") if default_branch else ()
                )
                self._run_git(
                    sources_root,
                    "clone",
                    *branch_args,
                    "--",
                    remote,
                    str(staging),
                )
                self._assert_safe_checkout(staging)
                if target.exists():
                    # The previous checkout is only discarded once its
                    # replacement is BOTH in place and recorded. Any failure in
                    # between puts the old tree back under its still-valid
                    # record, so a half-published replacement never costs the
                    # checkout that was working.
                    backup = Path(
                        tempfile.mkdtemp(prefix=f".{source_id}-replaced-", dir=sources_root)
                    )
                    backup.rmdir()
                    os.replace(target, backup)
                    try:
                        os.replace(staging, target)
                        published = True
                        self._write_source_record(sources_root, source_id, remote, default_branch)
                    except Exception:
                        if published:
                            os.replace(target, staging)
                            published = False
                        os.replace(backup, target)
                        raise
                    if not platform_compat.rmtree_force(backup):
                        raise ProjectGitError(
                            f"replaced Project repo source could not be removed: {source_id}"
                        )
                else:
                    os.replace(staging, target)
                    published = True
                    try:
                        self._write_source_record(sources_root, source_id, remote, default_branch)
                    except Exception:
                        # Nothing preceded this checkout; an unrecorded one
                        # must not be left to be mistaken for a recorded one.
                        os.replace(target, staging)
                        published = False
                        raise
                return target
            finally:
                if not published and staging.exists():
                    platform_compat.rmtree_force(staging)

    # Provenance of a derived checkout, written beside it at clone time. Read
    # back before a checkout is reused; the checkout itself is never reopened
    # for Git (its .git/config is agent-writable derived state), so the record
    # is what says which remote and branch the tree came from.
    _SOURCE_RECORD_MAX_BYTES = 16 * 1024

    @staticmethod
    def _source_record_path(sources_root: Path, source_id: str) -> Path:
        return sources_root / f"{source_id}.source.json"

    def _write_source_record(
        self, sources_root: Path, source_id: str, remote: str, default_branch: str
    ) -> None:
        record = self._source_record_path(sources_root, source_id)
        self._assert_derived_path_unlinked(record)
        payload = json.dumps(
            {"remote": remote, "default_branch": default_branch}, sort_keys=True
        ).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(prefix=f".{source_id}-record-", dir=sources_root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(tmp_name, record)
        except OSError:
            with suppress(OSError):
                os.unlink(tmp_name)
            raise

    def _checkout_matches(
        self, sources_root: Path, source_id: str, remote: str, default_branch: str
    ) -> bool:
        """True when the recorded provenance of an existing checkout matches its declaration.

        The source id is manifest-declared and independent of the URL and
        branch, so the record is what ties the tree to a declaration. A
        missing, unreadable, oversized, linked or mismatched record is a
        mismatch: the caller replaces the checkout with a fresh clone. A forged
        record can only choose between those two safe outcomes.
        """
        record = self._source_record_path(sources_root, source_id)
        fd: int | None = None
        try:
            fd = os.open(str(record), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_size > self._SOURCE_RECORD_MAX_BYTES
            ):
                return False
            with os.fdopen(fd, "rb") as handle:
                fd = None
                payload = handle.read(self._SOURCE_RECORD_MAX_BYTES + 1)
        except OSError:
            return False
        finally:
            if fd is not None:
                os.close(fd)
        if len(payload) > self._SOURCE_RECORD_MAX_BYTES:
            return False
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            return False
        if not isinstance(data, dict):
            return False
        return data.get("remote") == remote and data.get("default_branch") == default_branch

    def resolve_source(
        self,
        project_id: str,
        source_id: str,
        *,
        remote: str | None = None,
        default_branch: str = "",
        base_dir: Path | None = None,
    ) -> Path | None:
        """Return an existing derived checkout without cloning or fetching.

        With a declaration (``remote`` and ``default_branch``) the checkout is
        returned only when its provenance record matches it, normalized the
        same way ``materialize_source`` normalizes before recording. A
        mismatch answers ``None`` -- the read-only attachment path serves only a
        checkout the manifest currently declares, and it does not clone either;
        activation is where the owner reviews and re-clones.
        """
        if remote is not None:
            if not remote.strip():
                return None
            remote = self._validate_remote(remote, base_dir=base_dir)
            default_branch = self._validate_branch(default_branch)
        sources_root = self.registry.projects_dir / "state" / project_id / "sources"
        target = sources_root / source_id
        self._assert_derived_path_unlinked(target)
        with self._lock(f"{project_id}-{source_id}"):
            self._assert_derived_path_unlinked(target)
            if not target.exists():
                return None
            if not (target / ".git").exists():
                raise ProjectGitError(
                    f"Project repo source path is not a Git checkout: {source_id}"
                )
            self._assert_safe_checkout(target)
            if remote is not None and not self._checkout_matches(
                sources_root, source_id, remote, default_branch
            ):
                return None
            return target

    def remove_derived_state(self, project_id: str) -> None:
        """Remove regenerable source checkouts before unregistering a Project."""
        with self._lock(f"{project_id}-storage"):
            state_root = self.registry.projects_dir / "state" / project_id
            self._assert_derived_path_unlinked(state_root)
            if state_root.exists() and not platform_compat.rmtree_force(state_root):
                raise ProjectGitError("Project derived state could not be removed")

    def sync(self, identifier: str) -> RegisteredProject:
        """Fetch and fast-forward a managed clone without committing or pushing.

        Every Git coordinate comes from the registration pinned at add time
        (``remote`` and ``default_branch``, read through the hardened registry
        read). The checkout's own ``origin`` and ``HEAD`` are agent-writable
        derived state, so an agent that rewrites ``.git/config`` cannot choose
        what the owner's sync fetches: the fetch names the pinned URL and branch
        explicitly and everything downstream reads ``FETCH_HEAD``.
        """
        project = self.registry.resolve(identifier)
        managed = [
            registration
            for registration in project.registrations
            if registration.origin == "managed_git"
        ]
        if not managed:
            raise ProjectGitError(f"Project {project.id} has no managed Git clone")
        registration = managed[-1]
        if not registration.remote.strip():
            raise ProjectGitError(
                "managed Project clone has no pinned remote; remove and add it again"
            )
        if not registration.default_branch.strip():
            raise ProjectGitError(
                "managed Project clone has no pinned branch; remove and add it again"
            )
        remote = self._validate_remote(registration.remote)
        branch = self._validate_branch(registration.default_branch)
        with self._lock(f"{project.id}-bundle"):
            self._assert_safe_checkout(registration.path)
            self._run_git(registration.path, "fetch", "--", remote, branch)
            remote_manifest_ref = "FETCH_HEAD:project.yaml"
            remote_manifest_size_raw = self._run_git(
                registration.path, "cat-file", "-s", remote_manifest_ref
            ).stdout.strip()
            try:
                remote_manifest_size = int(remote_manifest_size_raw)
            except ValueError as exc:
                raise ProjectGitError("remote Project manifest size is invalid") from exc
            if remote_manifest_size > PROJECT_MANIFEST_MAX_BYTES:
                raise ProjectGitError("remote Project manifest is invalid: manifest is too large")
            try:
                remote_manifest = load_project_manifest_text(
                    self._run_git(
                        registration.path,
                        "show",
                        remote_manifest_ref,
                    ).stdout,
                    source=remote_manifest_ref,
                )
            except ProjectManifestError as exc:
                raise ProjectGitError(f"remote Project manifest is invalid: {exc}") from exc
            self._run_git(registration.path, "merge", "--ff-only", "FETCH_HEAD")
            synced = self.registry.refresh(project.id)
        # Outside the bundle lock: each source takes its own lock, and the pulled
        # manifest is what declares them. An existing checkout fast-forwards; one
        # whose declaration moved re-clones, with the previous tree surviving
        # until the new provenance record is written.
        self.sync_sources(synced.id, registration.path, remote_manifest)
        return synced
