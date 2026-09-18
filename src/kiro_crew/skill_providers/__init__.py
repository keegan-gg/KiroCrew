"""Multi-provider skill discovery and installation.

This package provides a pluggable interface for searching and installing
skills from external registries. Each provider (skills.sh, a GitHub repository,
PromptFarm, etc.) implements the ``SkillProvider`` protocol and registers itself
in the ``ProviderRegistry``.

Every provider's network layer comes from ``_http``: the SSRF screen, the
redirect allowlist and the bounded body read are one implementation, so a new
provider inherits the trust boundary instead of restating it.
"""

from kiro_crew.skill_providers.base import (
    ProviderRegistry,
    SkillProvider,
    SkillSearchResult,
)
from kiro_crew.skill_providers.github import GitHubRepoConfig, GitHubRepoProvider
from kiro_crew.skill_providers.skillsh import SkillsShProvider

__all__ = [
    "SkillProvider",
    "SkillSearchResult",
    "ProviderRegistry",
    "SkillsShProvider",
    "GitHubRepoProvider",
    "GitHubRepoConfig",
]
