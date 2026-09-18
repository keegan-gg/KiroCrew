"""Tests for the GitHub-repo skill provider (``skill_providers/github.py``).

Every HTTP call is mocked at this module's own ``_sync_fetch_json`` /
``_sync_fetch_text`` seams, so no test touches the network. The fake responses
mirror GitHub's real shapes: the ``sha`` media type answers with a bare SHA, the
git-trees endpoint answers with ``{"tree": [...], "truncated": bool}``, and the
raw host answers with file bytes.

What each class pins:

- addressing (``TestParseRepoSpec``) — the grammar, the pasted-URL affordance and
  every refusal, since a parse that accepts too much is what would put attacker
  text into a URL and then into a filesystem path;
- discovery (``TestSearch``) — several skills per repository, the commit pin
  travelling in every row's id, and a non-address query costing no request;
- bundles (``TestFetchBundle``) — the pin record, nested-skill exclusion, the
  size ceilings, the non-UTF-8 skip and the container refusal;
- trust posture (``TestNetworkGuards``, ``TestInstallNamespace``) — the shared
  SSRF screen and allowlist really are this provider's, and an install lands in
  the provider-prefixed namespace where it cannot shadow a shipped skill.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from kiro_crew.skill_providers import github as gh
from kiro_crew.skill_providers.base import SkillProvider

# ---- fixtures / fakes ------------------------------------------------------

_COMMIT = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"

_SKILL_MD = """---
name: reviewer
description: Reviews a diff for missing tests.
---

# Reviewer

Body text.
"""

_OTHER_SKILL_MD = """---
name: releaser
description: Cuts a release.
---

Body.
"""


def _tree(*paths_and_sizes: tuple[str, int], truncated: bool = False) -> dict:
    """A git-trees response carrying *paths_and_sizes* as blobs."""
    return {
        "sha": _COMMIT,
        "truncated": truncated,
        "tree": [
            {"path": path, "type": "blob", "size": size, "mode": "100644"}
            for path, size in paths_and_sizes
        ],
    }


class _Fake:
    """Routes the provider's two fetch seams to canned responses by URL substring.

    Recording every URL is deliberate: several tests assert on what was NOT
    requested (no call at all for a non-address query, no blob fetch for an
    oversized file), which a return-value-only stub cannot express.
    """

    def __init__(self, *, json_routes: dict, text_routes: dict) -> None:
        self.json_routes = json_routes
        self.text_routes = text_routes
        self.json_urls: list[str] = []
        self.text_urls: list[str] = []

    def fetch_json(self, url: str):
        self.json_urls.append(url)
        for fragment, payload in self.json_routes.items():
            if fragment in url:
                return payload
        return None

    def fetch_text(self, url: str, accept=None):
        self.text_urls.append(url)
        for fragment, payload in self.text_routes.items():
            if fragment in url:
                return payload if isinstance(payload, str) else None
        return None

    def fetch_bytes(self, url: str):
        """Blob fetches go through the bytes seam, not the text one.

        A route holding ``bytes`` is served verbatim, which is how a test supplies
        a body that is not valid UTF-8; a ``str`` route is encoded. An absent route
        is a FAILED fetch (``None``) -- a different thing from undecodable bytes,
        and the provider now treats them differently.
        """
        self.text_urls.append(url)
        for fragment, payload in self.text_routes.items():
            if fragment in url:
                return payload if isinstance(payload, bytes) else payload.encode()
        return None

    def install(self):
        return patch.multiple(
            gh,
            _sync_fetch_json=self.fetch_json,
            _sync_fetch_text=self.fetch_text,
            _sync_fetch_bytes=self.fetch_bytes,
        )


def _one_skill_repo() -> _Fake:
    """acme/widgets with a single skill at ``skills/reviewer``."""
    return _Fake(
        json_routes={
            "/git/trees/": _tree(
                ("SKILL.md", len(_SKILL_MD)),
                ("rules/tests.md", 40),
            )
        },
        text_routes={
            "/commits/": _COMMIT,
            "/SKILL.md": _SKILL_MD,
            "/rules/tests.md": "always ask for a test",
        },
    )


# ---- addressing -----------------------------------------------------------


class TestParseRepoSpec:
    """The address grammar is the provider's outermost gate: every later URL and
    every installed filesystem path is built from what it returns, so it must
    accept exactly the documented forms and nothing adjacent."""

    def test_bare_repo(self):
        spec = gh.parse_repo_spec("acme/widgets")
        assert spec is not None
        assert (spec.owner, spec.repo, spec.ref, spec.path) == ("acme", "widgets", "", "")

    def test_ref_only(self):
        spec = gh.parse_repo_spec("acme/widgets@v2.1")
        assert spec is not None
        assert spec.ref == "v2.1" and spec.path == ""

    def test_path_only(self):
        spec = gh.parse_repo_spec("acme/widgets:skills/reviewer")
        assert spec is not None
        assert spec.ref == "" and spec.path == "skills/reviewer"

    def test_ref_and_path(self):
        spec = gh.parse_repo_spec("acme/widgets@release/2:skills/reviewer")
        assert spec is not None
        assert spec.ref == "release/2" and spec.path == "skills/reviewer"

    def test_slashed_ref_survives_because_path_splits_first(self):
        # The ':' partition runs before the '@' one, so a branch name containing
        # '/' is unambiguous in the @ref form -- this is the documented way to
        # address one, and a pasted tree URL cannot express it.
        spec = gh.parse_repo_spec("acme/widgets@feature/a/b:skills/x")
        assert spec is not None
        assert spec.ref == "feature/a/b" and spec.path == "skills/x"

    @pytest.mark.parametrize(
        "raw",
        [
            "https://github.com/acme/widgets",
            "http://github.com/acme/widgets",
            "https://www.github.com/acme/widgets",
            "github.com/acme/widgets",
            "acme/widgets.git",
            "  acme/widgets/  ",
        ],
    )
    def test_url_and_suffix_forms_normalize(self, raw):
        spec = gh.parse_repo_spec(raw)
        assert spec is not None
        assert spec.repo_slug == "acme/widgets"

    def test_pasted_tree_url(self):
        spec = gh.parse_repo_spec("https://github.com/acme/widgets/tree/main/skills/reviewer")
        assert spec is not None
        assert spec.ref == "main" and spec.path == "skills/reviewer"

    def test_pasted_blob_url(self):
        spec = gh.parse_repo_spec("https://github.com/acme/widgets/blob/main/skills")
        assert spec is not None
        assert spec.ref == "main" and spec.path == "skills"

    def test_tree_url_combined_with_at_ref_is_refused(self):
        # Two refs in one address have no defensible resolution order, so it is
        # refused rather than silently resolved one way.
        assert gh.parse_repo_spec("acme/widgets/tree/main@v2") is None
        assert gh.parse_repo_spec("acme/widgets/tree/main:x") is None

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "acme",
            "acme/",
            "/widgets",
            "docker compose",  # a plain search term
            "react",
            "acme//widgets",
            "acme/widgets/notatree/main",
            "acme/widgets@..",
            "acme/widgets@a/../b",
            "acme/widgets:..",
            "acme/widgets:../../etc/passwd",
            "acme/widgets:skills/../../etc",
            "acme/widgets:/absolute",
            "acme/widgets:skills/a b",  # space is not in the segment grammar
            "acme/widgets@-bad",  # a ref segment may not start with '-'
            "-acme/widgets",  # nor may an owner
            "acme/widgets@ref?query=1",
            "acme/widgets:skills/x#frag",
            "acme/wid gets",
        ],
    )
    def test_refusals(self, raw):
        assert gh.parse_repo_spec(raw) is None

    def test_non_string_is_refused(self):
        # search() forwards whatever the caller passed; a non-string must not
        # reach .strip() and raise inside the aggregate fan-out.
        assert gh.parse_repo_spec(None) is None
        assert gh.parse_repo_spec(12) is None

    def test_address_round_trips(self):
        spec = gh.parse_repo_spec("acme/widgets@v2:skills/reviewer")
        assert spec is not None
        assert spec.address() == "acme/widgets@v2:skills/reviewer"
        assert gh.parse_repo_spec(spec.address()) == spec

    def test_address_substitutes_ref_and_path(self):
        spec = gh.parse_repo_spec("acme/widgets")
        assert spec is not None
        assert spec.address(ref="0a1b2c3", path="skills/x") == "acme/widgets@0a1b2c3:skills/x"


# ---- protocol conformance -------------------------------------------------


class TestProviderShape:
    def test_satisfies_the_provider_protocol(self):
        # Registration in _build_registry() only inherits the install plumbing if
        # the provider really is a SkillProvider; the registry's own structural
        # check would otherwise skip it at runtime.
        assert isinstance(gh.GitHubRepoProvider(), SkillProvider)

    def test_identity_and_availability(self):
        p = gh.GitHubRepoProvider()
        assert p.name == "github"
        assert p.display_name == "GitHub repo"
        assert p.api_base == "https://api.github.com"
        # Always available: unauthenticated, so nothing can be unconfigured. The
        # discovery policy gate in _build_registry is what disables the provider,
        # and test_a_refused_policy_keeps_the_provider_unregistered covers that.
        assert p.is_available()


# ---- discovery ------------------------------------------------------------


class TestSearch:
    @pytest.mark.asyncio
    async def test_non_address_query_costs_no_request(self):
        # This provider sits in the aggregate fan-out, so every unrelated search
        # reaches it. It must answer from the regex alone -- otherwise typing in
        # the Discover box would spend the unauthenticated GitHub rate limit.
        fake = _Fake(json_routes={}, text_routes={})
        with fake.install():
            assert await gh.GitHubRepoProvider().search("docker compose") == []
        assert fake.json_urls == [] and fake.text_urls == []

    @pytest.mark.asyncio
    async def test_several_skills_per_repo(self):
        fake = _Fake(
            json_routes={
                "/git/trees/": _tree(
                    ("skills/reviewer/SKILL.md", len(_SKILL_MD)),
                    ("skills/reviewer/rules/a.md", 10),
                    ("skills/releaser/SKILL.md", len(_OTHER_SKILL_MD)),
                    ("README.md", 20),
                )
            },
            text_routes={
                "/commits/": _COMMIT,
                "/skills/reviewer/SKILL.md": _SKILL_MD,
                "/skills/releaser/SKILL.md": _OTHER_SKILL_MD,
            },
        )
        with fake.install():
            results = await gh.GitHubRepoProvider().search("acme/widgets")

        # One row per directory holding a SKILL.md, in sorted order -- README.md
        # is not a skill and the nested rules file is not a second one.
        assert [r.name for r in results] == ["releaser", "reviewer"]
        assert [r.id for r in results] == [
            f"acme/widgets@{_COMMIT[:7]}:skills/releaser",
            f"acme/widgets@{_COMMIT[:7]}:skills/reviewer",
        ]

    @pytest.mark.asyncio
    async def test_row_carries_frontmatter_and_pinned_urls(self):
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", len(_SKILL_MD)))},
            text_routes={"/commits/": _COMMIT, "/SKILL.md": _SKILL_MD},
        )
        with fake.install():
            (row,) = await gh.GitHubRepoProvider().search("acme/widgets:skills/reviewer")

        # The loader's own frontmatter grammar, so the row matches what the
        # installed skill will show.
        assert row.name == "reviewer"
        assert row.description == "Reviews a diff for missing tests."
        assert row.provider == "github"
        assert row.author == "acme"
        # The FULL commit is browsable from the row; the id abbreviates it.
        assert row.repo_url == (f"https://github.com/acme/widgets/tree/{_COMMIT}/skills/reviewer")
        assert row.id == f"acme/widgets@{_COMMIT[:7]}:skills/reviewer"

    @pytest.mark.asyncio
    async def test_id_pins_the_resolved_commit_not_the_requested_branch(self):
        # The point of the pin: a row discovered from a branch must not send the
        # preview and the install back to that branch, which may have moved.
        fake = _one_skill_repo()
        with fake.install():
            (row,) = await gh.GitHubRepoProvider().search("acme/widgets@main:skills/reviewer")
        assert "@main" not in row.id
        assert row.id.split("@")[1].split(":")[0] == _COMMIT[:7]

    @pytest.mark.asyncio
    async def test_row_falls_back_to_directory_name_when_skill_md_unreadable(self):
        # The skill IS there (the tree says so); only its frontmatter is missing.
        # Dropping the row would hide an importable skill.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("skills/reviewer/SKILL.md", 10))},
            text_routes={"/commits/": _COMMIT},
        )
        with fake.install():
            (row,) = await gh.GitHubRepoProvider().search("acme/widgets")
        assert row.name == "reviewer"
        assert row.description == ""

    @pytest.mark.asyncio
    async def test_root_skill_falls_back_to_repo_name(self):
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10))},
            text_routes={"/commits/": _COMMIT},
        )
        with fake.install():
            (row,) = await gh.GitHubRepoProvider().search("acme/widgets")
        assert row.name == "widgets"
        assert row.id == f"acme/widgets@{_COMMIT[:7]}"

    @pytest.mark.asyncio
    async def test_limit_is_honoured(self):
        fake = _Fake(
            json_routes={"/git/trees/": _tree(*[(f"s{i}/SKILL.md", 10) for i in range(8)])},
            text_routes={"/commits/": _COMMIT},
        )
        with fake.install():
            results = await gh.GitHubRepoProvider().search("acme/widgets", limit=3)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_repo_ceiling_caps_a_huge_repo(self):
        fake = _Fake(
            json_routes={"/git/trees/": _tree(*[(f"s{i:03d}/SKILL.md", 10) for i in range(60)])},
            text_routes={"/commits/": _COMMIT},
        )
        with fake.install():
            results = await gh.GitHubRepoProvider().search("acme/widgets", limit=50)
        assert len(results) == gh._MAX_SKILLS_PER_REPO

    @pytest.mark.asyncio
    async def test_unresolvable_ref_yields_nothing(self):
        fake = _Fake(json_routes={}, text_routes={})  # /commits/ returns None
        with fake.install():
            assert await gh.GitHubRepoProvider().search("acme/widgets@nope") == []

    @pytest.mark.asyncio
    async def test_non_sha_commit_response_is_refused(self):
        # GitHub is external input: only a full 40-hex SHA may become part of a
        # URL and of the recorded pin.
        for bogus in ("<html>404</html>", "main", _COMMIT[:7], _COMMIT + "0"):
            fake = _Fake(
                json_routes={"/git/trees/": _tree(("SKILL.md", 10))},
                text_routes={"/commits/": bogus},
            )
            with fake.install():
                assert await gh.GitHubRepoProvider().search("acme/widgets") == []

    @pytest.mark.asyncio
    async def test_repo_without_any_skill_md_yields_nothing(self):
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("README.md", 10), ("src/a.py", 10))},
            text_routes={"/commits/": _COMMIT},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().search("acme/widgets") == []

    @pytest.mark.asyncio
    async def test_truncated_tree_is_refused(self):
        # A partial listing would install a skill missing files. Refusing is the
        # honest answer; a half-written skill is not.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10), truncated=True)},
            text_routes={"/commits/": _COMMIT},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().search("acme/widgets") == []

    @pytest.mark.asyncio
    async def test_malformed_tree_payload_yields_nothing(self):
        for payload in ([], "maintenance", 7, {"tree": None}, {"tree": "x"}, None):
            fake = _Fake(
                json_routes={"/git/trees/": payload},
                text_routes={"/commits/": _COMMIT},
            )
            with fake.install():
                assert await gh.GitHubRepoProvider().search("acme/widgets") == []

    @pytest.mark.asyncio
    async def test_unusable_tree_paths_are_never_requested(self):
        # Tree paths are external input that become both a URL and a filesystem
        # path. The guard's effect is that such a path is dropped BEFORE a
        # request is built -- asserting only on the resulting bundle would pass
        # even without it, because an unfetchable path is skipped anyway.
        fake = _Fake(
            json_routes={
                "/git/trees/": _tree(
                    ("SKILL.md", 10),
                    ("../escape.md", 10),
                    ("a/../../b.md", 10),
                    ("/abs.md", 10),
                    ("has space.md", 10),
                )
            },
            text_routes={"/commits/": _COMMIT, "/SKILL.md": _SKILL_MD},
        )
        with fake.install():
            bundle = await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets")
        assert bundle is not None
        assert [p for p, _ in bundle if p != gh.PIN_FILENAME] == ["SKILL.md"]
        # Nothing outside the grammar was turned into a URL at all.
        blob_urls = [u for u in fake.text_urls if "/commits/" not in u]
        assert blob_urls == [f"https://raw.githubusercontent.com/acme/widgets/{_COMMIT}/SKILL.md"]

    @pytest.mark.asyncio
    async def test_subpath_address_narrows_the_tree_request(self):
        # The tree is asked for as "<commit>:<path>", which both narrows the
        # response under the 1 MiB cap and makes the returned paths the bundle's
        # own relative paths.
        fake = _one_skill_repo()
        with fake.install():
            await gh.GitHubRepoProvider().search("acme/widgets:skills/reviewer")
        assert any(f"{_COMMIT}:skills/reviewer" in u for u in fake.json_urls)

    @pytest.mark.asyncio
    async def test_blobs_are_fetched_from_the_raw_host_pinned_to_the_commit(self):
        fake = _one_skill_repo()
        with fake.install():
            await gh.GitHubRepoProvider().search("acme/widgets:skills/reviewer")
        assert any(
            u.startswith(f"https://raw.githubusercontent.com/acme/widgets/{_COMMIT}/")
            for u in fake.text_urls
        )


# ---- bundles --------------------------------------------------------------


class TestFetchBundle:
    @pytest.mark.asyncio
    async def test_bundle_carries_files_and_the_pin_record(self):
        fake = _one_skill_repo()
        with fake.install():
            bundle = await gh.GitHubRepoProvider().fetch_skill_bundle(
                f"acme/widgets@{_COMMIT[:7]}:skills/reviewer"
            )
        assert bundle is not None
        paths = [p for p, _ in bundle]
        assert paths == ["SKILL.md", "rules/tests.md", gh.PIN_FILENAME]
        # Every entry is (str, str) so the install handler's c.encode("utf-8")
        # can never raise.
        assert all(isinstance(p, str) and isinstance(c, str) for p, c in bundle)

    @pytest.mark.asyncio
    async def test_pin_record_records_the_full_commit(self):
        fake = _one_skill_repo()
        with fake.install():
            bundle = await gh.GitHubRepoProvider().fetch_skill_bundle(
                f"acme/widgets@{_COMMIT[:7]}:skills/reviewer"
            )
        assert bundle is not None
        pin = json.loads(dict(bundle)[gh.PIN_FILENAME])
        assert pin["provider"] == "github"
        assert pin["repo"] == "acme/widgets"
        assert pin["repo_url"] == "https://github.com/acme/widgets"
        # The FULL 40-hex commit, not the abbreviation the address carried.
        assert pin["commit"] == _COMMIT
        assert pin["requested_ref"] == _COMMIT[:7]
        assert pin["path"] == "skills/reviewer"
        assert pin["source_url"] == (
            f"https://github.com/acme/widgets/tree/{_COMMIT}/skills/reviewer"
        )
        assert pin["files"] == ["SKILL.md", "rules/tests.md"]
        assert "none" in pin["tracking"]
        assert pin["imported_at"].endswith("Z")

    @pytest.mark.asyncio
    async def test_repo_copy_of_the_pin_filename_is_not_trusted(self):
        # A repository shipping our own sidecar name would otherwise be read back
        # as provenance we wrote.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10), (gh.PIN_FILENAME, 10))},
            text_routes={
                "/commits/": _COMMIT,
                "/SKILL.md": _SKILL_MD,
                f"/{gh.PIN_FILENAME}": '{"commit": "deadbeef", "repo": "evil/repo"}',
            },
        )
        with fake.install():
            bundle = await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets")
        assert bundle is not None
        assert [p for p, _ in bundle].count(gh.PIN_FILENAME) == 1
        assert json.loads(dict(bundle)[gh.PIN_FILENAME])["commit"] == _COMMIT

    @pytest.mark.asyncio
    async def test_nested_skill_files_are_excluded(self):
        # Importing a directory that holds both its own SKILL.md and a nested
        # skill must take only its own files -- the nested one is a separate
        # import with its own pin.
        fake = _Fake(
            json_routes={
                "/git/trees/": _tree(
                    ("SKILL.md", 10),
                    ("rules/a.md", 10),
                    ("nested/SKILL.md", 10),
                    ("nested/rules/b.md", 10),
                )
            },
            text_routes={
                "/commits/": _COMMIT,
                "/SKILL.md": _SKILL_MD,
                "/rules/a.md": "mine",
                "/nested/SKILL.md": _OTHER_SKILL_MD,
                "/nested/rules/b.md": "theirs",
            },
        )
        with fake.install():
            bundle = await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets")
        assert bundle is not None
        assert [p for p, _ in bundle] == ["SKILL.md", "rules/a.md", gh.PIN_FILENAME]

    @pytest.mark.asyncio
    async def test_container_without_its_own_skill_md_is_refused(self):
        # An address naming a directory that only CONTAINS skills is not a skill.
        # Refusing keeps one install from raking in every skill in the repo.
        #
        # The root-level ``notes.md`` is what makes the refusal observable: it is
        # not excluded as a nested skill's file, so without the up-front refusal
        # it would be fetched and the container address would cost a request
        # before failing later for a different reason.
        fake = _Fake(
            json_routes={
                "/git/trees/": _tree(("a/SKILL.md", 10), ("b/SKILL.md", 10), ("notes.md", 10))
            },
            text_routes={
                "/commits/": _COMMIT,
                "/a/SKILL.md": _SKILL_MD,
                "/notes.md": "loose notes",
            },
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None
        # Refused on the listing alone -- no blob was read.
        assert [u for u in fake.text_urls if "/commits/" not in u] == []

    @pytest.mark.asyncio
    async def test_agents_md_only_bundle_is_accepted(self):
        # The install writer copies AGENTS.md to SKILL.md, so a repo using that
        # convention imports fine.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("AGENTS.md", 10))},
            text_routes={"/commits/": _COMMIT, "/AGENTS.md": _SKILL_MD},
        )
        with fake.install():
            bundle = await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets")
        assert bundle is not None
        assert [p for p, _ in bundle] == ["AGENTS.md", gh.PIN_FILENAME]

    @pytest.mark.asyncio
    async def test_oversized_blob_refuses_the_bundle_without_fetching_it(self):
        # Skipping it would install a skill whose own instructions may reference
        # the missing file, and report success. The tree already states the size,
        # so the refusal costs no request.
        fake = _Fake(
            json_routes={
                "/git/trees/": _tree(("SKILL.md", 10), ("huge.bin", gh._MAX_BUNDLE_BYTES + 1))
            },
            text_routes={"/commits/": _COMMIT, "/SKILL.md": _SKILL_MD},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None
        assert not any("huge.bin" in u for u in fake.text_urls)
        assert not any("SKILL.md" in u for u in fake.text_urls if "/commits/" not in u)

    @pytest.mark.asyncio
    async def test_running_total_ceiling_refuses_the_bundle(self):
        # The cap is the RUNNING total, not a per-file verdict: three individually
        # legal files must not add up past it. Truncating at the ceiling would be
        # the same partial install by another route.
        half = "x" * (gh._MAX_BUNDLE_BYTES // 2 + 10)
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10), ("a.md", 10), ("b.md", 10))},
            text_routes={
                "/commits/": _COMMIT,
                "/SKILL.md": _SKILL_MD,
                "/a.md": half,
                "/b.md": half,
            },
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_file_count_ceiling_refuses_the_bundle(self):
        # Taking the first N would drop the rest silently -- the same defect as a
        # dropped oversized file, so it gets the same answer.
        many = [(f"f{i:03d}.md", 10) for i in range(gh._MAX_BUNDLE_FILES + 20)]
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10), *many)},
            text_routes={"/commits/": _COMMIT, ".md": "body"},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_a_bundle_at_the_file_ceiling_is_accepted(self):
        # The ceiling is a ceiling, not an off-by-one refusal.
        many = [(f"f{i:03d}.md", 10) for i in range(gh._MAX_BUNDLE_FILES - 1)]
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10), *many)},
            text_routes={"/commits/": _COMMIT, "/SKILL.md": _SKILL_MD, ".md": "body"},
        )
        with fake.install():
            bundle = await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets")
        assert bundle is not None
        assert len([p for p, _ in bundle if p != gh.PIN_FILENAME]) == gh._MAX_BUNDLE_FILES

    @pytest.mark.asyncio
    async def test_undecodable_blob_refuses_the_bundle(self):
        # The bundle contract is text, so a binary asset cannot ride it -- and a
        # skill whose instructions reference an image it did not get is broken in a
        # way the user only discovers later. Refuse, naming the file.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10), ("logo.png", 10))},
            text_routes={
                "/commits/": _COMMIT,
                "/SKILL.md": _SKILL_MD,
                "/logo.png": b"\x89PNG\r\n\x1a\n\xff\xfe",
            },
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_a_failed_fetch_refuses_the_bundle(self):
        # Distinct from the case above: the file exists and we could not read it.
        # A text-only fetch collapses both to None, which is why blobs are read as
        # bytes and decoded here.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10), ("rules/a.md", 10))},
            text_routes={"/commits/": _COMMIT, "/SKILL.md": _SKILL_MD},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_case_colliding_paths_refuse_the_bundle(self):
        # macOS and Windows fold case: two entries here, one file there, and the
        # second write silently replaces the first. Re-importing reproduces it.
        fake = _Fake(
            json_routes={
                "/git/trees/": _tree(("SKILL.md", 10), ("Rules.md", 10), ("rules.md", 10))
            },
            text_routes={
                "/commits/": _COMMIT,
                "/SKILL.md": _SKILL_MD,
                "/Rules.md": "upper",
                "/rules.md": "lower",
            },
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_an_empty_instruction_file_refuses_the_bundle(self):
        # An empty SKILL.md installs a skill the loader can see and the agent
        # cannot use.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 0))},
            text_routes={"/commits/": _COMMIT, "/SKILL.md": ""},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_bundle_without_an_instruction_file_is_refused(self):
        # Every file failed to fetch: an install of just the pin record would
        # create a skill the loader cannot discover.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10))},
            text_routes={"/commits/": _COMMIT},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_bad_address_is_refused_before_any_request(self):
        fake = _Fake(json_routes={}, text_routes={})
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("../../etc") is None
        assert fake.json_urls == [] and fake.text_urls == []

    @pytest.mark.asyncio
    async def test_fetch_skill_content_prefers_skill_md(self):
        fake = _Fake(
            json_routes={
                "/git/trees/": _tree(("AGENTS.md", 10), ("README.md", 10), ("SKILL.md", 10))
            },
            text_routes={
                "/commits/": _COMMIT,
                "/SKILL.md": _SKILL_MD,
                "/AGENTS.md": _OTHER_SKILL_MD,
                "/README.md": "readme",
            },
        )
        with fake.install():
            content = await gh.GitHubRepoProvider().fetch_skill_content("acme/widgets")
        assert content == _SKILL_MD

    @pytest.mark.asyncio
    async def test_fetch_skill_content_falls_back_to_agents_md(self):
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("AGENTS.md", 10), ("README.md", 10))},
            text_routes={
                "/commits/": _COMMIT,
                "/AGENTS.md": _OTHER_SKILL_MD,
                "/README.md": "readme",
            },
        )
        with fake.install():
            content = await gh.GitHubRepoProvider().fetch_skill_content("acme/widgets")
        assert content == _OTHER_SKILL_MD

    @pytest.mark.asyncio
    async def test_fetch_skill_content_returns_none_for_a_bad_address(self):
        fake = _Fake(json_routes={}, text_routes={})
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_content("nope") is None


# ---- trust posture --------------------------------------------------------


class TestNetworkGuards:
    """The SSRF screen and the redirect allowlist are shared code, but they are
    only a control for THIS provider if this provider's bindings really call
    them with its own allowlist and audit label."""

    def test_internal_addresses_are_blocked(self):
        for url in (
            "http://169.254.169.254/latest/meta-data/",
            "http://0xa9fea9fe/",  # hex metadata endpoint
            "http://2852039166/",  # decimal metadata endpoint
            "http://127.0.0.1/x",
            "http://localhost/x",
            "http://10.0.0.5/",
            "http://[::1]/",
            "http:///no-host",
        ):
            assert gh._is_internal_url(url) is True, f"not blocked: {url}"

    def test_github_hosts_pass_the_screen(self):
        assert gh._is_internal_url("https://api.github.com/repos/a/b") is False
        assert gh._is_internal_url(f"https://raw.githubusercontent.com/a/b/{_COMMIT}/S.md") is False

    def test_allowlist_is_exact_and_https_only(self):
        assert gh._is_allowed_host("https://api.github.com/repos/a/b")
        assert gh._is_allowed_host("https://raw.githubusercontent.com/a/b/c/S.md")
        assert gh._is_allowed_host("https://codeload.github.com/a/b/tar.gz/c")
        # Plain HTTP, a lookalike suffix and an arbitrary DNS name all fail.
        assert not gh._is_allowed_host("http://api.github.com/repos/a/b")
        assert not gh._is_allowed_host("https://api.github.com.evil.example/x")
        assert not gh._is_allowed_host("https://evil-api.github.com.co/x")
        assert not gh._is_allowed_host("https://metadata.google.internal/x")
        assert not gh._is_allowed_host("https://169.254.169.254/x")

    def test_blocked_internal_ip_emits_a_sel_audit_for_this_provider(self):
        with patch.object(gh, "_audit_ssrf_blocked") as m:
            assert gh._is_internal_url("http://0xa9fea9fe/") is True
            assert m.called, "blocked SSRF attempt did not emit a SEL audit event"

    def test_allowed_host_does_not_emit_an_audit(self):
        with patch.object(gh, "_audit_ssrf_blocked") as m:
            assert gh._is_internal_url("https://api.github.com/x") is False
            assert not m.called

    def test_fetch_seams_pass_this_providers_allowlist(self):
        # A provider that reached the shared fetch with the WRONG allowlist would
        # look identical in every behavioural test above, so pin the wiring.
        with patch.object(gh._http, "sync_fetch_json", return_value={"ok": 1}) as m:
            gh._sync_fetch_json("https://api.github.com/x")
        assert m.call_args.kwargs["allowed_hosts"] is gh._ALLOWED_HOSTS
        assert m.call_args.kwargs["internal_check"] is gh._is_internal_url

        with patch.object(gh._http, "sync_fetch_text", return_value="x") as m:
            gh._sync_fetch_text("https://raw.githubusercontent.com/x", "text/plain")
        assert m.call_args.kwargs["allowed_hosts"] is gh._ALLOWED_HOSTS
        assert m.call_args.kwargs["internal_check"] is gh._is_internal_url

    def test_commit_resolution_asks_for_the_sha_media_type(self):
        # The commit OBJECT carries the commit's whole file list, which for a
        # large merge exceeds the 1 MiB cap and would fail to resolve a good ref.
        fake = _one_skill_repo()
        seen: list[str | None] = []

        def _text(url, accept=None):
            seen.append(accept)
            return fake.fetch_text(url, accept)

        async def _run():
            with patch.multiple(gh, _sync_fetch_json=fake.fetch_json, _sync_fetch_text=_text):
                await gh.GitHubRepoProvider().search("acme/widgets:skills/reviewer")

        import asyncio as _asyncio

        _asyncio.run(_run())
        assert "application/vnd.github.sha" in seen


class TestWriterCompatibility:
    """Every path this module accepts must be one the install writer will write.

    The writer (``discover.py``'s ``_write_bundle``) drops a path containing
    ``..``, one starting with ``/``, and one starting with ``./..`` -- silently,
    with no log. A path accepted here and dropped there installs a skill missing a
    file while reporting success, so the grammar has to be the STRICTER of the two.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "foo..bar.md",  # `..` inside a name, not as a segment
            "a/foo..bar/b.md",
            "..",
            "../escape.md",
            "a/../b.md",
            "/absolute.md",
            "./../x.md",
            "notes.",  # Windows strips a trailing dot -> two entries, one file
            "dir./a.md",
            "a//b.md",
            "a/",
            "/",
            "has space.md",
            "quote'.md",
            "semi;colon.md",
            "star*.md",
            "pipe|.md",
            "colon:name.md",
        ],
    )
    def test_paths_the_writer_would_drop_are_refused_here(self, path):
        assert gh._valid_relative_path(path) is False

    @pytest.mark.parametrize(
        "path",
        [
            "SKILL.md",
            "rules/tests.md",
            ".gitignore",
            "scripts/run.sh",
            "a/b/c/d.md",
            "-dash.md",  # safe to write; refusing it would block an ordinary repo
            "_under.md",
            "v1.2.3/notes.md",
        ],
    )
    def test_ordinary_repository_paths_are_accepted(self, path):
        assert gh._valid_relative_path(path) is True

    def test_the_root_is_accepted(self):
        assert gh._valid_relative_path("") is True

    def test_an_absurdly_long_path_is_refused_before_the_pattern_runs(self):
        assert gh._valid_relative_path("a/" * 5000 + "b.md") is False

    def test_an_absurdly_long_ref_is_refused(self):
        assert gh._valid_ref("a/" * 5000 + "b") is False


class TestAddressLengthBound:
    """The install key is the address run through the handler's ``_slugify``, which
    TRUNCATES at 64 characters. Two skills sharing a 64-character prefix would land
    on one key, and an overwrite there deletes the first."""

    def test_slug_length_equals_address_length(self):
        # This is the fact ``_MAX_ADDRESS_CHARS`` rests on, and it belongs to the
        # handler, not to this module -- so assert it against the real function.
        from kiro_crew.dashboard.handlers.discover import _slugify

        for address in (
            "acme/widgets",
            f"acme/widgets@{_COMMIT[:7]}",
            f"acme/widgets@{_COMMIT[:7]}:skills/reviewer",
            f"a-b.c/d_e.f@{_COMMIT[:7]}:g/h-i/j.k",
        ):
            assert len(_slugify(address)) == len(address), address

    def test_a_row_whose_key_would_truncate_is_not_offered(self):
        owner = "o" * 39
        repo = "r" * 90
        long_dir = "d" * 30
        fake = _Fake(
            json_routes={
                "/git/trees/": _tree((f"{long_dir}/SKILL.md", 10), ("short/SKILL.md", 10))
            },
            text_routes={"/commits/": _COMMIT, "SKILL.md": _SKILL_MD},
        )

        async def _run():
            with fake.install():
                return await gh.GitHubRepoProvider().search(f"{owner}/{repo}")

        import asyncio as _asyncio

        results = _asyncio.run(_run())
        # Both addresses blow the budget on the owner/repo prefix alone, so neither
        # row is offered -- better than two rows that install over each other.
        assert results == []

    def test_a_short_address_is_still_offered(self):
        fake = _one_skill_repo()

        async def _run():
            with fake.install():
                return await gh.GitHubRepoProvider().search("acme/widgets:skills/reviewer")

        import asyncio as _asyncio

        (row,) = _asyncio.run(_run())
        assert len(row.id) <= gh._MAX_ADDRESS_CHARS

    @pytest.mark.asyncio
    async def test_a_hand_typed_over_budget_address_refuses_the_bundle(self):
        # Discovery drops such a row, but nothing stops a user pasting one.
        owner = "o" * 39
        repo = "r" * 90
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10))},
            text_routes={"/commits/": _COMMIT, "/SKILL.md": _SKILL_MD},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle(f"{owner}/{repo}") is None


class TestInstallNamespace:
    """Where an import lands, expressed against the install handler's own key
    derivation rather than restated -- decision: an imported skill may never
    shadow a shipped one."""

    def test_key_is_provider_prefixed_and_cannot_shadow_a_shipped_skill(self):
        from kiro_crew.dashboard.handlers.discover import _SAFE_SLUG_RE, _slugify

        provider = gh.GitHubRepoProvider()
        skill_id = f"acme/widgets@{_COMMIT[:7]}:skills/reviewer"
        slug = _slugify(skill_id)
        # The handler demands a separator-free slug, so the key can only ever be
        # one segment under the provider's own directory.
        assert _SAFE_SLUG_RE.match(slug)
        key = f"{provider.name}/{slug}"
        assert key.startswith("github/")
        assert key.count("/") == 1
        # A shipped skill's key has no "github/" prefix, so no import can occupy
        # it; re-importing the same address hits the handler's 409 instead.
        assert slug == "acme-widgets-0a1b2c3-skills-reviewer"

    def test_two_repos_with_the_same_skill_name_do_not_collide(self):
        from kiro_crew.dashboard.handlers.discover import _slugify

        a = _slugify(f"acme/widgets@{_COMMIT[:7]}:skills/reviewer")
        b = _slugify(f"other/tools@{_COMMIT[:7]}:skills/reviewer")
        assert a != b

    def test_registry_registers_the_provider_under_its_vetted_name(self):
        from kiro_crew.dashboard.handlers import discover

        registry = discover._build_registry()
        assert "github" in registry.provider_names
        assert isinstance(registry.get("github"), gh.GitHubRepoProvider)
