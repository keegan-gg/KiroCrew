"""GitHub-repo skill provider — import a SKILL.md bundle pinned to one commit.

The user already keeps skills in their own repositories; this provider makes such
a repository addressable from the same Discover surface as a public registry, so
the whole install path — the preview, the 1 MiB response cap, the SSRF screen,
the redirect allowlist, the traversal-proof bundle writer and the HUMAN-ONLY
install gate in ``dashboard/handlers/discover.py`` — is inherited rather than
rebuilt. Nothing here is a second way into the skills directory.

**Import, not subscription.** An install copies the files in and the user owns the
result; the copy is pinned to the commit it came from and nothing re-syncs it.
That posture is the whole reason this is safe to ship without a review step: a
repository that changes under the user cannot change an already-imported skill,
because no code ever looks upstream again. Re-running the import is the update
path, and it goes through the same human-only gate as the first one. Branch
tracking / re-sync is deliberately absent (see #746 for the adjacent design).

**Addressing** — ``owner/repo[@ref][:path]``:

- ``acme/widgets`` — every skill in the repository, at the default branch.
- ``acme/widgets@v2`` — at the tag/branch/commit ``v2``.
- ``acme/widgets:skills/reviewer`` — one skill directory.
- ``acme/widgets@v2:skills/reviewer`` — both.
- ``https://github.com/acme/widgets/tree/main/skills/reviewer`` — a pasted tree
  URL is accepted as the same thing. Its first segment after ``tree`` is the ref,
  so a branch name containing ``/`` must use the ``@ref`` form instead.

A *skill* is any directory holding a ``SKILL.md``, so one repository can carry
many; discovery returns one row per directory and each row's id names exactly
that directory.

**What a row's id pins.** Discovery resolves the ref to a commit once and puts the
abbreviated commit in every row's id, so the preview and the install fetch the
commit discovery actually showed instead of re-resolving a branch that may have
moved in between. A hand-typed address naming a branch is resolved at fetch time
instead, and either way the FULL commit is recorded in the installed copy's
``.skill-import-source.json``.

**A bundle is complete, or it is refused.** Every reason a file could be left out
-- a failed fetch, a body that is not UTF-8, a per-file or total size ceiling, a
file count over the ceiling, two names that collide where case is ignored, an
install key that would be truncated -- refuses the whole import and logs why,
rather than writing a subset. A skill missing a file its own instructions
reference does not fail at import; it fails later, somewhere else, as a puzzle.
The only deliberate omissions are ``_EXCLUDED_NAMES``, which are never skill
content. ``fetch_skill_bundle`` can only answer ``None``, so the reason reaches
the log and not yet the user -- an error channel on the Protocol is follow-up work.

**Rate limit.** Requests are unauthenticated, which GitHub limits to 60 per hour
per IP; one discover/preview/install cycle costs two API calls plus one raw fetch
per file. Authenticated fetches (and caching the resolved tree) are follow-up
work, not a gap in the trust posture.
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Iterable

from kiro_crew.frontmatter import SKILL_LOADER, parse_frontmatter
from kiro_crew.skill_providers import _http
from kiro_crew.skill_providers.base import SkillSearchResult

logger = logging.getLogger(__name__)

# GitHub REST API base (no trailing slash).
_API_BASE = "https://api.github.com"

# Raw file host. Blob contents are read from here rather than through the
# contents API: the raw host returns the bytes directly, so a file does not
# arrive base64-inflated inside a JSON envelope that the 1 MiB cap then has to
# cover, and the URL is pinned to the resolved commit.
_RAW_BASE = "https://raw.githubusercontent.com"

# Hosts a fetch may be REDIRECTED to. The initial URL is always built from
# ``_API_BASE`` or ``_RAW_BASE`` above and is additionally screened by
# ``_is_internal_url``. Keep this list tight: add a host only for a concrete,
# observed redirect target.
_ALLOWED_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "media.githubusercontent.com",
        "codeload.github.com",
    }
)

# Address grammar. Every part is validated before it reaches a URL: the owner and
# repository shapes are GitHub's own, a ref may not contain a path-traversal or
# an empty segment, and a path segment is held to the same characters the skills
# loader can represent on every platform.
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]{0,99}$")
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Dots in a segment are ISOLATED by construction -- never
# adjacent, never trailing -- which is load-bearing three times over:
#
# * ``..`` cannot appear anywhere, as a whole segment OR inside a name. A
#   per-segment ``!= ".."`` test admits ``foo..bar.md``, and the install writer's
#   blunter ``".." in rel_path`` guard then drops that file SILENTLY -- accepted
#   here, never written, so the skill installs incomplete and reports success.
# * a trailing dot cannot appear. Windows strips one, so ``notes.`` and ``notes``
#   would name ONE file on disk while reading as two distinct entries here.
# * the alternation is unambiguous (``.``, ``/`` and the name class are disjoint),
#   so the nested quantifiers cannot backtrack catastrophically; the length caps
#   below bound the input regardless.
# A FILE or directory segment. A leading dash is allowed: it is perfectly safe to
# write, and under the complete-or-refused rule below, refusing it would make an
# ordinary repository unimportable over a filename nobody chose deliberately.
_PATH_SEGMENT = r"\.?[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*"

# A REF segment is stricter -- no leading dash -- because a ref is typed by a
# person and reads like an option when it starts with one. Nothing downstream
# passes it as an argument, so this is about the address being unambiguous rather
# than about an injection.
_REF_SEGMENT = r"\.?[A-Za-z0-9_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_][A-Za-z0-9_-]*)*"

# Whole path and ref, each matched in ONE go rather than split on "/": an empty,
# leading, trailing or doubled separator is refused by the shape itself, and no
# filesystem-separator assumption is expressed anywhere.
_RELATIVE_PATH_RE = re.compile(rf"^{_PATH_SEGMENT}(?:/{_PATH_SEGMENT})*$")
_REF_RE = re.compile(rf"^{_REF_SEGMENT}(?:/{_REF_SEGMENT})*$")

# An owner/repo address, optionally in pasted tree/blob-URL form.
_ADDRESS_RE = re.compile(
    r"^(?P<owner>[^/@:]+)/(?P<repo>[^/@:]+)(?:/(?P<kind>tree|blob)/(?P<rest>.+))?$"
)

# Length ceilings applied BEFORE the patterns run, so a regex never sees an
# unbounded string.
_MAX_PATH_CHARS = 512
_MAX_REF_CHARS = 256

# The install handler derives an installed skill's on-disk key by running its own
# ``_slugify`` over the id and TRUNCATING at 64 characters. Two skills whose
# addresses share a 64-character prefix would land on ONE key, where an overwrite
# deletes the first -- so an address longer than that budget is refused rather
# than emitted. Every character of a valid address is either kept verbatim by
# ``_slugify`` or is one of ``/``, ``@``, ``:``, each becoming a single ``-`` and
# never adjacent to another, so the slug is exactly as long as the address and
# this one number is the whole check. ``test_slug_length_equals_address_length``
# pins that equality against the real ``_slugify`` rather than trusting it here.
_MAX_ADDRESS_CHARS = 64

# Prefixes stripped so a pasted repository or tree URL parses as an address.
_URL_PREFIXES = (
    "https://github.com/",
    "http://github.com/",
    "https://www.github.com/",
    "www.github.com/",
    "github.com/",
)

# Discovery ceilings. A repository may hold any number of skills; a search
# response is bounded so one address cannot turn into an unbounded fan of raw
# fetches, and the caller's own ``limit`` narrows it further.
_MAX_SKILLS_PER_REPO = 20

# One bundle's ceilings. ``_MAX_BUNDLE_BYTES`` is the running total across every
# file, deliberately equal to the per-response cap: the discover handler's own
# guard is 5 MiB, so this is the one that binds first.
_MAX_BUNDLE_FILES = 50
_MAX_BUNDLE_BYTES = _http.MAX_RESPONSE_BYTES

# The pin record written into every imported skill directory. Dot-prefixed to
# match the loader's other sidecar (``.builtin-skill-provenance``) and so it
# never reads as part of the skill's own content.
PIN_FILENAME = ".skill-import-source.json"

# Files a skill directory may carry that must never be imported: a repository's
# own copy of our pin record would otherwise be mistaken for provenance we
# wrote, and git plumbing is not skill content.
_EXCLUDED_NAMES = frozenset({PIN_FILENAME, ".gitattributes", ".gitmodules"})


@dataclass(frozen=True)
class RepoSpec:
    """One parsed ``owner/repo[@ref][:path]`` address."""

    owner: str
    repo: str
    ref: str = ""
    """Requested ref — a branch, tag, or (abbreviated) commit. '' = default branch."""

    path: str = ""
    """Repository-relative directory. '' = repository root."""

    @property
    def repo_slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"

    def address(self, *, ref: str | None = None, path: str | None = None) -> str:
        """Re-render this address, optionally substituting the ref or path."""
        use_ref = self.ref if ref is None else ref
        use_path = self.path if path is None else path
        out = f"{self.owner}/{self.repo}"
        if use_ref:
            out += f"@{use_ref}"
        if use_path:
            out += f":{use_path}"
        return out


def _valid_relative_path(path: str) -> bool:
    """True iff *path* is a safe repository-relative directory or file path.

    ``''`` is the repository root. Everything else must match the whole-path
    pattern, which admits no traversal, no empty/leading/trailing/doubled
    separator, no URL delimiter and no trailing dot -- ``_PATH_SEGMENT`` records
    why each of those matters to the install writer.
    """
    if not path:
        return True  # the repository root
    return len(path) <= _MAX_PATH_CHARS and bool(_RELATIVE_PATH_RE.match(path))


def _valid_ref(ref: str) -> bool:
    """True iff *ref* is a safe git ref to place in a URL path."""
    if not ref:
        return True  # default branch
    return len(ref) <= _MAX_REF_CHARS and bool(_REF_RE.match(ref))


def parse_repo_spec(raw: Any) -> RepoSpec | None:
    """Parse one address into a :class:`RepoSpec`, or None if it is not one.

    None is the answer for every string that is not a repository address, which
    is what makes this provider safe to leave in the aggregate search: a plain
    search term ("docker compose") parses to None and costs no network call.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    lowered = text.lower()
    for prefix in _URL_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.strip("/")
    if not text:
        return None

    head, has_path, path = text.partition(":")
    repo_part, has_ref, ref = head.partition("@")
    matched = _ADDRESS_RE.match(repo_part)
    if matched is None:
        return None
    owner, repo = matched.group("owner"), matched.group("repo")

    if matched.group("kind") is not None:
        # A pasted tree/blob URL: owner/repo/tree/<ref>/<path...>. It carries its
        # own ref and path, so combining it with @ref or :path is ambiguous and
        # refused rather than silently resolved one way. The first remaining
        # segment is the ref and the rest is the path, which is why a branch name
        # containing "/" needs the @ref form.
        if has_ref or has_path:
            return None
        ref, _, path = matched.group("rest").partition("/")

    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    # rstrip only: a TRAILING slash is a harmless paste artefact ("…/reviewer/"),
    # but stripping a LEADING one would silently turn the malformed ":/absolute"
    # into the perfectly valid relative "absolute". An address that starts at the
    # root is refused below, not corrected.
    ref = ref.rstrip("/")
    path = path.rstrip("/")

    if not _OWNER_RE.match(owner) or not _REPO_RE.match(repo):
        return None
    if not _valid_ref(ref) or not _valid_relative_path(path):
        return None
    return RepoSpec(owner=owner, repo=repo, ref=ref, path=path)


@dataclass
class GitHubRepoConfig:
    """Configuration for the GitHub-repo provider.

    There is deliberately no ``enabled`` flag. The composed ``discovery`` policy
    gate in ``_build_registry`` is the off-switch -- a provider it refuses is never
    registered, so it has no search, preview or install path left -- and a second
    way to disable one provider is a second thing that has to stay true.
    """

    # The platform ``discovery`` policy allowlists a source by URL, so this is
    # the identity a managed deployment writes its allowlist against. It is not
    # user-settable: every URL this module builds starts here or at ``_RAW_BASE``,
    # both of which are in ``_ALLOWED_HOSTS``.
    api_base: str = _API_BASE


class GitHubRepoProvider:
    """Provider that imports skills from a GitHub repository, pinned to a commit."""

    def __init__(self, config: GitHubRepoConfig | None = None) -> None:
        self._config = config or GitHubRepoConfig()

    @property
    def api_base(self) -> str:
        """The API base this provider fetches from — the policy allowlist key."""
        return self._config.api_base

    @property
    def name(self) -> str:
        return "github"

    @property
    def display_name(self) -> str:
        return "GitHub repo"

    def is_available(self) -> bool:
        """Always ready: it needs no credential, and the policy gate is the switch.

        The provider reaches GitHub unauthenticated, so there is no configuration
        that could be missing and nothing to probe. ``provider_available`` still
        calls this because it is protocol surface every provider carries.
        """
        return True

    async def search(self, query: str, *, limit: int = 20) -> list[SkillSearchResult]:
        """Resolve *query* as a repository address and list the skills it holds.

        This provider has no catalog to search: a query that is not an address
        yields nothing without touching the network, so leaving it in the
        aggregate fan-out costs a regex on every unrelated search.
        """
        spec = parse_repo_spec(query)
        if spec is None:
            return []

        commit = await self._resolve_commit(spec)
        if commit is None:
            return []
        blobs = await self._list_blobs(spec, commit)
        if not blobs:
            return []

        skill_dirs = _skill_directories(blobs)
        if not skill_dirs:
            return []
        capped = skill_dirs[: max(1, min(limit, _MAX_SKILLS_PER_REPO))]

        # One raw fetch per skill, concurrently: the frontmatter is what gives a
        # row its real name and description, and it is the same file the preview
        # and the install will read. A failed fetch degrades that row to its
        # directory name rather than dropping it — the skill is genuinely there.
        heads = await asyncio.gather(*[self._fetch_skill_md(spec, commit, d) for d in capped])

        results: list[SkillSearchResult] = []
        for skill_dir, head in zip(capped, heads, strict=True):
            full_path = posixpath.join(spec.path, skill_dir) if skill_dir else spec.path
            address = spec.address(ref=commit[:7], path=full_path)
            if len(address) > _MAX_ADDRESS_CHARS:
                # Emitting it would hand two skills one truncated install key, so
                # the row is dropped rather than offered as installable.
                logger.warning(
                    "Skipping %r: its install key would exceed %d characters",
                    address,
                    _MAX_ADDRESS_CHARS,
                )
                continue
            fallback_name = posixpath.basename(full_path) or spec.repo
            meta = _frontmatter_of(head)
            results.append(
                SkillSearchResult(
                    id=address,
                    name=meta.get("name") or fallback_name,
                    description=meta.get("description", ""),
                    provider=self.name,
                    # Carries the FULL pinned commit and is directly viewable, so
                    # the row itself shows what a row's id abbreviates.
                    repo_url=_tree_url(spec, commit, full_path),
                    author=spec.owner,
                    tags=[],
                )
            )
        return results

    async def fetch_skill_content(self, skill_id: str) -> str | None:
        """Fetch one skill's instruction file. See ``fetch_skill_bundle``."""
        bundle = await self.fetch_skill_bundle(skill_id)
        if bundle is None:
            return None
        # Same precedence the install writer applies (SKILL.md, else AGENTS.md
        # copied to SKILL.md, else any markdown), so a preview reads the file the
        # installed skill will actually expose.
        for wanted in ("SKILL.md", "AGENTS.md"):
            named = next((f for f in bundle if f[0] == wanted), None)
            if named:
                return named[1]
        any_md = next((f for f in bundle if f[0].endswith(".md")), None)
        return any_md[1] if any_md else None

    async def fetch_skill_bundle(self, skill_id: str) -> list[tuple[str, str]] | None:
        """Fetch one skill directory as ``(relative_path, content)`` pairs.

        *skill_id* must address a single skill — a directory that itself holds a
        ``SKILL.md``. Files belonging to a NESTED skill are excluded, so
        importing a repository's root when it carries several skills cannot rake
        all of them into one install.

        The last entry is always :data:`PIN_FILENAME`, recording the full commit
        this content came from. It is appended here rather than written by the
        install handler so the preview lists exactly the files the install will
        write, and a repository's own file of that name is dropped instead of
        being trusted as provenance.
        """
        spec = parse_repo_spec(skill_id)
        if spec is None:
            return None
        commit = await self._resolve_commit(spec)
        if commit is None:
            return None
        blobs = await self._list_blobs(spec, commit)
        if not blobs:
            return None

        own_files = _own_skill_files(blobs)
        if own_files is None:
            logger.debug(
                "GitHub address %r holds no SKILL.md at its root; refusing bundle",
                spec.address(),
            )
            return None

        if len(spec.address()) > _MAX_ADDRESS_CHARS:
            return _refuse(spec, f"its install key would exceed {_MAX_ADDRESS_CHARS} characters")

        wanted = [
            (path, size)
            for path, size in own_files
            if posixpath.basename(path) not in _EXCLUDED_NAMES
        ]
        if not wanted:
            return _refuse(spec, "it holds no importable file")
        if len(wanted) > _MAX_BUNDLE_FILES:
            return _refuse(
                spec, f"it holds {len(wanted)} files, over the {_MAX_BUNDLE_FILES} ceiling"
            )
        oversized = [path for path, size in wanted if size > _MAX_BUNDLE_BYTES]
        if oversized:
            return _refuse(spec, f"{oversized[0]!r} is over the {_MAX_BUNDLE_BYTES}-byte ceiling")
        collision = _case_collision(path for path, _ in wanted)
        if collision:
            return _refuse(spec, collision)

        fetched = await asyncio.gather(
            *[self._fetch_blob(spec, commit, path) for path, _ in wanted]
        )

        bundle: list[tuple[str, str]] = []
        total = 0
        for (path, _), raw in zip(wanted, fetched, strict=True):
            # None is a FAILED fetch, which is distinct from bytes that are not
            # text: the first means the file exists and we could not read it, and
            # installing without it would report success for a skill missing a
            # file its own instructions may reference.
            if raw is None:
                return _refuse(spec, f"{path!r} could not be fetched")
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                return _refuse(spec, f"{path!r} is not UTF-8 text")
            total += len(raw)
            if total > _MAX_BUNDLE_BYTES:
                return _refuse(spec, f"its files total over {_MAX_BUNDLE_BYTES} bytes")
            bundle.append((path, content))

        if not any(path in ("SKILL.md", "AGENTS.md") and content for path, content in bundle):
            return _refuse(spec, "its instruction file is missing or empty")

        bundle.append((PIN_FILENAME, _pin_record(spec, commit, bundle)))
        return bundle

    # ---- GitHub plumbing -------------------------------------------------

    async def _resolve_commit(self, spec: RepoSpec) -> str | None:
        """Resolve *spec*'s ref to a full commit SHA, or None.

        Asks for the ``sha`` media type rather than the commit object: the object
        carries the commit's whole file list, which for a large merge exceeds the
        1 MiB response cap and would fail to resolve a perfectly good ref.
        ``HEAD`` stands for the repository's default branch, so the default case
        costs no extra request.
        """
        ref = spec.ref or "HEAD"
        url = (
            f"{self._config.api_base}/repos/"
            f"{urllib.parse.quote(spec.owner)}/{urllib.parse.quote(spec.repo)}"
            f"/commits/{urllib.parse.quote(ref, safe='/')}"
        )
        raw = await _fetch_text(url, accept="application/vnd.github.sha")
        if not isinstance(raw, str):
            return None
        sha = raw.strip().lower()
        # GitHub is external input: only a full 40-hex SHA may become part of a
        # URL and of the recorded pin.
        return sha if _FULL_SHA_RE.match(sha) else None

    async def _list_blobs(self, spec: RepoSpec, commit: str) -> list[tuple[str, int]] | None:
        """List ``(path, size)`` for every blob under *spec*'s path at *commit*.

        Paths are relative to ``spec.path``, because the tree is requested as
        ``<commit>:<path>`` when a path is given — that both narrows the response
        under the 1 MiB cap and makes the returned paths the bundle's own
        relative paths.

        A ``truncated`` tree is refused outright. Using a partial listing would
        silently install a skill missing files, which is worse than reporting
        that the repository is too large to import.
        """
        tree_ref = f"{commit}:{spec.path}" if spec.path else commit
        url = (
            f"{self._config.api_base}/repos/"
            f"{urllib.parse.quote(spec.owner)}/{urllib.parse.quote(spec.repo)}"
            f"/git/trees/{urllib.parse.quote(tree_ref, safe=':/')}?recursive=1"
        )
        data = await _fetch_json(url)
        if not isinstance(data, dict):
            return None
        if data.get("truncated") is True:
            logger.warning(
                "GitHub tree for %r is truncated; refusing a partial import",
                spec.address(),
            )
            return None
        entries = data.get("tree")
        if not isinstance(entries, list):
            return None

        blobs: list[tuple[str, int]] = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "blob":
                continue
            path = entry.get("path")
            if not isinstance(path, str) or not path:
                continue
            # The tree is external input and its paths become filesystem paths,
            # so hold each one to the address grammar. The install writer checks
            # containment again; this keeps a refused path from being fetched.
            if not _valid_relative_path(path):
                logger.debug("Skipping unusable GitHub tree path: %r", path)
                continue
            try:
                size = int(entry.get("size", 0) or 0)
            except (TypeError, ValueError):
                size = 0
            blobs.append((path, size))
        return blobs

    async def _fetch_blob(self, spec: RepoSpec, commit: str, rel_path: str) -> bytes | None:
        """Fetch one blob's raw bytes from the raw host, pinned to *commit*.

        Bytes rather than text so the caller can tell a FAILED fetch (``None``)
        from a body that is simply not UTF-8 -- one is an error to refuse on, the
        other a fact about the file, and a text fetch collapses both to ``None``.
        """
        full_path = posixpath.join(spec.path, rel_path) if spec.path else rel_path
        return await _fetch_bytes(_raw_url(spec, commit, full_path))

    async def _fetch_skill_md(self, spec: RepoSpec, commit: str, skill_dir: str) -> str | None:
        """Fetch the SKILL.md of one discovered skill directory, or None.

        Discovery only wants the frontmatter, so a file that is unreadable or not
        text degrades that ROW to its directory name; unlike an install, showing
        a skill that is really there costs nothing.
        """
        rel = posixpath.join(skill_dir, "SKILL.md") if skill_dir else "SKILL.md"
        raw = await self._fetch_blob(spec, commit, rel)
        if raw is None:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None


def _refuse(spec: RepoSpec, because: str) -> "list[tuple[str, str]] | None":
    """Log why an import is refused and return None, the Protocol's only answer.

    ``fetch_skill_bundle`` can say nothing but ``None``, which the install handler
    renders as "not found or empty". The reason therefore lives in the log; giving
    the Protocol an error channel so the user reads it instead is follow-up work.
    A refusal is always preferred to a partial install: a skill missing a file it
    references fails later, somewhere else, as a puzzle.
    """
    logger.warning("Refusing GitHub skill %r: %s", spec.address(), because)
    return None  # typed as the bundle's Optional so callers can `return _refuse(...)`


def _case_collision(paths: "Iterable[str]") -> str:
    """Why two of *paths* would name one file, or '' when all are distinct.

    macOS and Windows fold case, so ``Rules.md`` and ``rules.md`` are two entries
    here and one file there -- the second write silently replaces the first, and
    re-importing reproduces it deterministically. The grammar is ASCII-only, so
    case folding is the whole of the equivalence; no Unicode normalisation applies.
    """
    seen: dict[str, str] = {}
    for path in paths:
        folded = path.lower()
        if folded in seen:
            return f"{path!r} and {seen[folded]!r} name one file where case is ignored"
        seen[folded] = path
    return ""


def _skill_directories(blobs: list[tuple[str, int]]) -> list[str]:
    """Directories (relative to the listed root) that hold a ``SKILL.md``.

    ``''`` denotes the listed root itself. Sorted so discovery order is stable
    across calls rather than following GitHub's tree order.
    """
    return sorted(
        {posixpath.dirname(path) for path, _ in blobs if posixpath.basename(path) == "SKILL.md"}
    )


def _own_skill_files(blobs: list[tuple[str, int]]) -> list[tuple[str, int]] | None:
    """Files belonging to the skill at the listed root, or None if there is none.

    A repository root holding several skills is a container, not a skill: every
    row discovery returns names a leaf directory, so this only refuses a
    hand-typed address, and refusing is what keeps one install from raking in
    every skill in the repository.
    """
    if not any(path in ("SKILL.md", "AGENTS.md") for path, _ in blobs):
        return None
    nested = {
        directory
        for directory in _skill_directories(blobs)
        if directory  # the root's own SKILL.md is not a nested skill
    }
    return sorted(
        (path, size)
        for path, size in blobs
        if not any(path.startswith(f"{directory}/") for directory in nested)
    )


def _frontmatter_of(content: str | None) -> dict[str, str]:
    """Parse *content*'s frontmatter with the skills loader's own grammar.

    Using the loader's grammar is what makes a discovered row's name and
    description equal the installed skill's — a second parser would disagree on
    block scalars. Repository content is untrusted, so a parse failure yields an
    empty mapping rather than propagating.
    """
    if not content:
        return {}
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    try:
        meta = parse_frontmatter(normalized, SKILL_LOADER)
    except Exception:
        logger.debug("Unparseable SKILL.md frontmatter from GitHub", exc_info=True)
        return {}
    if not isinstance(meta, dict):
        return {}
    return {k: v for k, v in meta.items() if isinstance(k, str) and isinstance(v, str)}


def _tree_url(spec: RepoSpec, commit: str, path: str) -> str:
    """A browsable URL for exactly the imported content, pinned to *commit*."""
    base = f"{spec.repo_url}/tree/{commit}"
    return f"{base}/{path}" if path else base


def _raw_url(spec: RepoSpec, commit: str, path: str) -> str:
    """The raw-content URL for one file, pinned to *commit*."""
    # safe="/" keeps the separators as real path segments while still encoding
    # "?", "#", space and the rest -- the grammar has already refused anything
    # that could smuggle a query string in.
    return (
        f"{_RAW_BASE}/{urllib.parse.quote(spec.owner)}/{urllib.parse.quote(spec.repo)}"
        f"/{commit}/{urllib.parse.quote(path, safe='/')}"
    )


def _pin_record(spec: RepoSpec, commit: str, bundle: list[tuple[str, str]]) -> str:
    """The JSON provenance written beside an imported skill.

    It records what the copy IS rather than what it should become: the exact
    commit, the address it was requested by, the browsable URL and the files
    taken. Nothing reads it back to re-sync — it exists so a user (or a later
    re-import) can tell where a skill came from and whether it has moved on.
    """
    record = {
        "provider": "github",
        "repo": spec.repo_slug,
        "repo_url": spec.repo_url,
        "requested_ref": spec.ref,
        "commit": commit,
        "path": spec.path,
        "source_url": _tree_url(spec, commit, spec.path),
        "imported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": [path for path, _ in bundle],
        "tracking": (
            "none — this is a pinned copy you own. Re-import the same address to "
            "pick up newer upstream content."
        ),
    }
    return json.dumps(record, indent=2, sort_keys=True) + "\n"


# ---- this provider's binding of the shared network guards -----------------


def _audit_ssrf_blocked(url: str, host: str, canonical_host: str) -> None:
    """Emit a SEL audit event for a blocked SSRF-to-internal-IP attempt here."""
    _http.audit_ssrf_blocked("github", url, host, canonical_host)


def _is_internal_url(url: str) -> bool:
    """This provider's binding of the shared internal-address screen."""
    return _http.is_internal_url(url, audit=_audit_ssrf_blocked)


def _is_allowed_host(url: str) -> bool:
    """True iff *url* is HTTPS on a host this provider may be redirected to."""
    return _http.is_allowed_host(url, _ALLOWED_HOSTS)


def _sync_fetch_json(url: str) -> Any | None:
    """Blocking, bounded, SSRF-screened JSON fetch."""
    return _http.sync_fetch_json(
        url,
        allowed_hosts=_ALLOWED_HOSTS,
        internal_check=_is_internal_url,
        headers={"Accept": "application/vnd.github+json"},
    )


def _sync_fetch_text(url: str, accept: str | None = None) -> str | None:
    """Blocking, bounded, SSRF-screened UTF-8 text fetch."""
    return _http.sync_fetch_text(
        url,
        allowed_hosts=_ALLOWED_HOSTS,
        internal_check=_is_internal_url,
        headers={"Accept": accept} if accept else None,
    )


def _sync_fetch_bytes(url: str) -> bytes | None:
    """Blocking, bounded, SSRF-screened raw fetch."""
    return _http.sync_fetch_bytes(
        url,
        allowed_hosts=_ALLOWED_HOSTS,
        internal_check=_is_internal_url,
    )


async def _fetch_json(url: str) -> Any | None:
    """Off-loop JSON fetch. Calls the module global so a test can patch it."""
    return await _http.run_off_loop(lambda: _sync_fetch_json(url))


async def _fetch_text(url: str, *, accept: str | None = None) -> str | None:
    """Off-loop text fetch. Calls the module global so a test can patch it."""
    return await _http.run_off_loop(lambda: _sync_fetch_text(url, accept))


async def _fetch_bytes(url: str) -> bytes | None:
    """Off-loop raw fetch. Calls the module global so a test can patch it."""
    return await _http.run_off_loop(lambda: _sync_fetch_bytes(url))
