"""GitProjectStore add/sync against a real local Git bundle, thin manifest."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from project_git_helpers import local_git_remote, requires_local_git_remote  # noqa: F401

from kiro_crew.project_git import GitProjectStore, ProjectGitError
from kiro_crew.project_registry import ProjectRegistry

# Every remote here is a local path or ``file://`` URL built from ``tmp_path``.
pytestmark = requires_local_git_remote


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "PATH": "/usr/bin:/bin",
            "HOME": str(cwd),
        },
    )


def _make_bundle_repo(root: Path) -> Path:
    repo = root / "bundle-remote"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "project.yaml").write_text(
        "apiVersion: crew.kiro/v1\n"
        "kind: Project\n"
        "name: e2e\n"
        "sources:\n"
        "  - type: repo\n"
        "    url: https://example.com/primary\n"
        "    role: primary\n",
        encoding="utf-8",
    )
    _git(repo, "add", "project.yaml")
    _git(repo, "commit", "-m", "init")
    return repo


@pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git not installed",
)
def test_add_clones_and_registers_managed_bundle(tmp_path: Path) -> None:
    remote = _make_bundle_repo(tmp_path)
    registry = ProjectRegistry(
        projects_dir=tmp_path / "projects",
        registry_dir=tmp_path / "projects-registry",
    )
    store = GitProjectStore(registry)
    try:
        project = store.add(f"file://{remote}")
    except ProjectGitError as exc:
        if "sandbox" in str(exc).lower():
            pytest.skip(f"sandbox unavailable in this environment: {exc}")
        raise

    managed = [r for r in project.registrations if r.origin == "managed_git"]
    assert managed, "add did not create a managed registration"
    reg = managed[-1]
    # Coordinates are pinned from the fresh clone, into the fenced registry.
    assert reg.remote == f"file://{remote}"
    assert reg.default_branch == "main"
    assert reg.path.exists()
    assert (reg.path / "project.yaml").exists()
    # The managed clone lives under the visible projects/ leaf, keyed by id.
    assert (tmp_path / "projects" / "managed") in reg.path.parents


@pytest.fixture
def bundle_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Use a local clone double while exercising registry publication and reuse."""
    import shutil

    from kiro_crew.project_manifest import create_project_manifest

    remote = tmp_path / "remote"
    create_project_manifest(remote, name="bundle")
    registry = ProjectRegistry(
        projects_dir=tmp_path / "projects",
        registry_dir=tmp_path / "projects-registry",
    )
    store = GitProjectStore(registry)
    branch = {"name": "main"}

    def git(cwd: Path, *args: str):
        if args[0] == "clone":
            shutil.copytree(remote, args[-1], dirs_exist_ok=True)
            return subprocess.CompletedProcess(args, 0, stdout="")
        assert args == ("symbolic-ref", "--quiet", "--short", "HEAD")
        return subprocess.CompletedProcess(args, 0, stdout=branch["name"])

    monkeypatch.setattr(store, "_run_git", git)
    monkeypatch.setattr(store, "_assert_safe_checkout", lambda _path: None)
    return store, remote, branch


def test_managed_readd_preserves_identity_checkout_and_review(bundle_store) -> None:
    store, remote, _branch = bundle_store
    project = store.add(str(remote))
    target = project.registrations[-1].path
    assert target == store.registry.projects_dir / "managed" / project.id / "bundle"
    project = store.registry.record_review(project.id, "sha256:reviewed", {"project.yaml": "hash"})
    (target / "local-work.txt").write_text("keep", encoding="utf-8")
    again = store.add(f" {remote}/../remote ")
    assert again == project
    assert (target / "local-work.txt").read_text(encoding="utf-8") == "keep"
    assert store.registry.list_projects() == (project,)
    assert not list((store.registry.projects_dir / "managed").glob("project-clone-*"))


def test_managed_same_manifest_different_coordinates_get_distinct_ids(bundle_store) -> None:
    store, remote, branch = bundle_store
    first = store.add(str(remote))
    second = store.add(str(remote.parent / "another-remote"))
    branch["name"] = "next"
    third = store.add(str(remote))
    assert len({first.id, second.id, third.id}) == 3
    assert len(store.registry.list_projects()) == 3


def test_unregistered_clone_survives_until_new_registration_is_durable(
    bundle_store, monkeypatch
) -> None:
    store, remote, _branch = bundle_store
    first = store.add(str(remote))
    old_target = first.registrations[-1].path
    store.registry.unregister(first.id)

    def fail_save(_projects):
        assert old_target.is_dir()
        raise OSError("registry unavailable")

    with monkeypatch.context() as patched:
        patched.setattr(store.registry, "_save_unlocked", fail_save)
        with pytest.raises(OSError, match="registry unavailable"):
            store.add(str(remote))
    assert old_target.is_dir()
    assert store.registry.list_projects() == ()
    second = store.add(str(remote))
    assert second.id != first.id
    assert second.registrations[-1].path.is_dir()
    assert old_target.is_dir()


def test_sync_updates_manifest_without_changing_registration_identity(
    bundle_store, monkeypatch
) -> None:
    from kiro_crew.project_manifest import load_project_manifest

    store, remote, _branch = bundle_store
    project = store.add(str(remote))
    target = project.registrations[-1].path
    project = store.registry.record_review(project.id, "sha256:reviewed", {"project.yaml": "hash"})
    text = "apiVersion: crew.kiro/v1\nkind: Project\nname: renamed\nsources: []\n"
    calls = []

    def git(cwd: Path, *args: str):
        assert cwd == target
        calls.append(args)
        if args == ("cat-file", "-s", "FETCH_HEAD:project.yaml"):
            output = str(len(text.encode("utf-8")))
        elif args == ("show", "FETCH_HEAD:project.yaml"):
            output = text
        elif args == ("merge", "--ff-only", "FETCH_HEAD"):
            (target / "project.yaml").write_text(text, encoding="utf-8")
            output = ""
        else:
            assert args == ("fetch", "--", str(remote), "main")
            output = ""
        return subprocess.CompletedProcess(args, 0, stdout=output)

    monkeypatch.setattr(store, "_run_git", git)
    synced = store.sync(project.id)
    assert synced.id == project.id
    assert synced.name == "renamed"
    assert synced.registrations == project.registrations
    assert synced.reviewed_digest == project.reviewed_digest
    assert synced.reviewed_files == project.reviewed_files
    assert load_project_manifest(target).name == "renamed"
    assert calls[-1] == ("merge", "--ff-only", "FETCH_HEAD")
