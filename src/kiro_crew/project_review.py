"""The reviewed-bundle digest over a Project's executable surfaces.

A Project's manifest is not the only thing a session start executes. kiro-cli
reads discovery surfaces out of the session cwd's own ``.kiro/`` directly --
``.kiro/settings/mcp.json`` carries full server definitions, command lines
included, and ``.kiro/agents/`` may carry ``mcpServers`` -- and ``sync``
fast-forwards a linked repository with no review step. So ``.kiro/`` is part of
the reviewed bundle: the digest the owner previews and accepts covers it
alongside ``project.yaml``; ``sync`` recomputes it, and a Project whose digest
moved is *review stale* until the owner looks again.

The scope is deliberately NOT an allowlist of the files kiro-cli reads today.
kiro-cli owns its discovery surfaces and can add one -- hooks, a skill's
scripts -- without Crew noticing, and a digest that enumerated files would let
that new surface through by default. Default-stale over the whole of ``.kiro/``
fails the other way: a file Crew does not know about moves the digest and the
owner reviews it. The carve-out is exactly :data:`REVIEW_TEXT_ONLY_RELDIRS`.

Text surfaces inherit kiro-cli's repository-trust posture by decision.
``.kiro/steering`` and the checkout's documents outside ``.kiro/`` reach a
session as instructions kiro-cli loads from any directory it runs in, under the
same posture as a directory the user opened by hand. ``.kiro/skills`` is under
the digest like every other non-steering ``.kiro/`` path -- a skill can carry
scripts -- and additionally keeps its own per-directory consent grant.

Absent files hash as absent rather than being skipped, so DELETING a reviewed
definition is a change the owner is shown, not a silent return to a clean
digest. A checkout with no ``.kiro/`` has nothing to gate: its digest is the
manifest's alone. The digest stops at the PRIMARY checkout, which is the only
directory a Project session ever runs in; ``reference`` sources are materialized
beside it and are never a kiro-cli workspace root.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from kiro_crew.project_manifest import PROJECT_MANIFEST_NAME

#: Bounded like the manifest read: an executable definition that does not fit is
#: refused review rather than trusted unread.
REVIEW_FILE_MAX_BYTES = 256 * 1024
#: A hostile commit cannot make review unaffordable by adding thousands of
#: files; past this many the digest records the overflow instead of the contents.
REVIEW_FILE_LIMIT = 512

#: The whole discovery tree kiro-cli reads out of a session's cwd.
REVIEW_ROOT_RELDIR = ".kiro"
#: The ONLY carve-out from :data:`REVIEW_ROOT_RELDIR`. Instructions, covered by
#: the RFC's text-surface decision: they reach a session exactly as they would
#: from a directory the user opened by hand, and the agent-side defence (injected
#: text is data, not instructions) is the one every session already relies on.
#: Everything else under ``.kiro/`` is treated as able to run code. Widening this
#: is a security decision, so ``test_project_review.py`` pins it to this exact
#: value and any change to it shows up as a diff on that test.
REVIEW_TEXT_ONLY_RELDIRS: tuple[str, ...] = (".kiro/steering",)

#: The two surfaces whose executable content motivated the gate. Retained as
#: documentation of what is known to run, NOT as the digest's scope.
MCP_SETTINGS_RELPATH = ".kiro/settings/mcp.json"
AGENTS_RELDIR = ".kiro/agents"

_ABSENT = "absent"
_UNREADABLE = "unreadable"
_OVERFLOW = "overflow"


def _hash_file(root: Path, relpath: str, contents: dict[str, bytes] | None = None) -> str:
    """Digest one reviewed file through the hardened link-refusing reader."""
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

    try:
        content = safe_read_file_bytes_nolink(
            str(root / relpath),
            within_root=str(root),
            max_bytes=REVIEW_FILE_MAX_BYTES,
        )
    except FileTooLargeError:
        # Oversized counts as CHANGED, never as unchanged: an unreadable
        # executable definition must not inherit a reviewed digest.
        return f"{_UNREADABLE}:too-large"
    except OSError:
        return f"{_UNREADABLE}:error"
    if content is None:
        # Missing, a link, or not a regular file. A link where a reviewed file
        # belongs is not "absent" -- it is a different thing than what was
        # reviewed, so it hashes distinctly.
        if not (root / relpath).exists():
            return _ABSENT
        return f"{_UNREADABLE}:not-a-regular-file"
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return f"{_UNREADABLE}:binary"
    if contents is not None:
        contents[relpath] = content
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _is_text_only(relpath: str) -> bool:
    """True when *relpath* is inside the text-only carve-out."""
    return any(
        relpath == carve_out or relpath.startswith(f"{carve_out}/")
        for carve_out in REVIEW_TEXT_ONLY_RELDIRS
    )


def _reviewed_relpaths(checkout: Path) -> tuple[list[str], int]:
    """Every reviewed file under the checkout's ``.kiro/``, sorted and bounded.

    Returns the paths to hash and the TOTAL found, so a tree past the cap records
    its size instead of its contents and growing past the cap still moves the
    digest.

    Walked with ``os.walk(followlinks=False)``: a symlinked directory is never
    descended into, so a link cannot enumerate an external tree under paths the
    digest would attribute to this checkout. The link itself is still RECORDED --
    it hashes to an unreadable marker, which :func:`unreviewable_files` keeps
    permanently stale -- because kiro-cli would follow it and load a target the
    digest cannot see. Skipping it would make it an invisible surface, which is
    the opposite of what this gate is for.
    """
    root_dir = checkout / REVIEW_ROOT_RELDIR
    if root_dir.is_symlink():
        # The discovery root itself replaced by a link: recorded, never walked.
        return [REVIEW_ROOT_RELDIR], 1
    try:
        if not root_dir.is_dir():
            return [], 0
    except OSError:
        return [], 0
    found: list[str] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(str(root_dir), followlinks=False):
        try:
            rel_here = Path(dirpath).relative_to(checkout).as_posix()
        except ValueError:
            dirnames[:] = []
            continue
        kept: list[str] = []
        for name in sorted(dirnames):
            relpath = f"{rel_here}/{name}"
            if _is_text_only(relpath):
                continue
            try:
                if (Path(dirpath) / name).is_symlink():
                    total += 1
                    if len(found) < REVIEW_FILE_LIMIT:
                        found.append(relpath)
                    continue
            except OSError:
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            relpath = f"{rel_here}/{name}"
            if _is_text_only(relpath):
                continue
            total += 1
            if len(found) < REVIEW_FILE_LIMIT:
                found.append(relpath)
    return sorted(found), total


def review_file_hashes(
    bundle_dir: Path,
    checkout_dir: Path | None,
    *,
    contents: dict[str, bytes] | None = None,
) -> dict[str, str]:
    """The reviewed surface as ``relative path -> content hash``.

    ``project.yaml`` is keyed from the bundle; ``.kiro/`` is keyed from the
    PRIMARY checkout, which is the directory a session actually runs in. When the
    primary checkout IS the bundle (a Project declaring no sources), both come
    from the same tree and the keys stay distinct.
    """
    hashes = {PROJECT_MANIFEST_NAME: _hash_file(bundle_dir, PROJECT_MANIFEST_NAME, contents)}
    if checkout_dir is None:
        return hashes
    relpaths, total = _reviewed_relpaths(checkout_dir)
    if total > REVIEW_FILE_LIMIT:
        hashes[REVIEW_ROOT_RELDIR] = f"{_OVERFLOW}:{total}"
    for relpath in relpaths:
        hashes[relpath] = _hash_file(checkout_dir, relpath, contents)
    return hashes


def review_digest(hashes: dict[str, str]) -> str:
    """One stable digest over the reviewed surface.

    Keyed on the path as well as the content so moving a definition between
    files, or deleting one, changes the digest.
    """
    material = "\n".join(f"{path}\0{digest}" for path, digest in sorted(hashes.items()))
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def compute_review_digest(
    bundle_dir: Path, checkout_dir: Path | None
) -> tuple[str, dict[str, str]]:
    """Return the reviewed digest and the per-file hashes it covers."""
    hashes = review_file_hashes(bundle_dir, checkout_dir)
    return review_digest(hashes), hashes


def changed_review_files(reviewed: dict[str, str], current: dict[str, str]) -> tuple[str, ...]:
    """Relative paths whose reviewed hash differs from the current one.

    Includes paths present on only one side, so both an added and a removed
    executable definition are named to the owner.
    """
    return tuple(
        sorted(
            path
            for path in set(reviewed) | set(current)
            if reviewed.get(path, _ABSENT) != current.get(path, _ABSENT)
        )
    )


def unreviewable_files(current: dict[str, str]) -> tuple[str, ...]:
    """Reviewed paths whose current state can never be an accepted baseline.

    A link, an oversized or non-UTF-8 file, a read error or a file-cap overflow
    hashes to a marker rather than to its complete content. A link's target or an unhashed
    file beyond the cap can change without moving the marker. Such paths stay
    stale on every check until the entire surface is readable and within bounds.
    """
    return tuple(
        sorted(
            path for path, digest in current.items() if digest.startswith((_UNREADABLE, _OVERFLOW))
        )
    )


def stale_review_paths(
    reviewed_digest: str, reviewed: dict[str, str], current: dict[str, str]
) -> tuple[str, ...]:
    """One stale-set definition shared by attachment, health and preview."""
    if reviewed_digest:
        changed = changed_review_files(reviewed, current)
    else:
        changed = tuple(
            path
            for path, digest in current.items()
            if path != PROJECT_MANIFEST_NAME and digest != _ABSENT
        )
    return tuple(sorted(set(changed) | set(unreviewable_files(current))))


def review_preview(
    bundle_dir: Path,
    checkout_dir: Path | None,
    reviewed_digest: str,
    reviewed: dict[str, str],
) -> tuple[dict, dict[str, str]]:
    """Preview the exact bytes hashed in this read, not a second filesystem read.

    Acceptance hashes again and compares the owner's shown digest. Every readable
    file is UTF-8 and fits the shared read cap, so its full text is displayed.
    A marker is never rendered as file content or accepted as a baseline.
    """
    contents: dict[str, bytes] = {}
    hashes = review_file_hashes(bundle_dir, checkout_dir, contents=contents)
    files = []
    for path in stale_review_paths(reviewed_digest, reviewed, hashes):
        value = hashes.get(path, _ABSENT)
        entry = {"path": path}
        if value.startswith((_UNREADABLE, _OVERFLOW)):
            suffix = value.partition(":")[2]
            entry["status"] = "unreadable"
            entry["reason"] = (
                "overflow"
                if value.startswith(_OVERFLOW)
                else (
                    suffix
                    if suffix in {"too-large", "binary"}
                    else "link-outside-root" if suffix == "not-a-regular-file" else "error"
                )
            )
        elif value == _ABSENT:
            entry["status"] = "removed"
        else:
            entry["status"] = (
                "added"
                if not reviewed_digest or reviewed.get(path, _ABSENT) == _ABSENT
                else "changed"
            )
            entry["content"] = contents[path].decode("utf-8")
        files.append(entry)
    return {"digest": review_digest(hashes), "files": files}, hashes
