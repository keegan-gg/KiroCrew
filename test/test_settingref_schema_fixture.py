"""Cross-layer drift guard for SettingRef schema keys and environment names.

Every generated configKey must exist in the backend schema. The Decisions
control additionally pins its literal key, TypeScript constant and point name.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from kiro_crew.config.schema import SCHEMA_REGISTRY

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = ROOT / "website" / "src" / "test" / "fixtures" / "settingref-schema.json"
ENV_VARS_FIXTURE_PATH = ROOT / "website" / "src" / "test" / "fixtures" / "settingref-env-vars.json"
SETTINGS_REGISTRY_PATH = (
    ROOT / "website" / "src" / "components" / "commandPalette" / "settingsRegistry.gen.ts"
)
BACKEND_SRC_ROOT = ROOT / "src" / "kiro_crew"
CONFIG_KEY_RE = re.compile(r'"configKey"\s*:\s*"([^"]+)"')
DECISIONS_READER_PATH = ROOT / "website" / "src" / "pages" / "settings" / "decisionsPreview.ts"
FEATURE_PREVIEWS_PATH = (
    ROOT / "website" / "src" / "pages" / "settings" / "FeaturePreviewsSection.tsx"
)
TS_CONST_RE = r"export const {name} = '([^']+)'"


@pytest.fixture()
def fixture_entries() -> list[dict]:
    """Load the shared JSON fixture used by both vitest and this test."""
    assert FIXTURE_PATH.exists(), f"Fixture not found: {FIXTURE_PATH}"
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def registry_index() -> dict[str, object]:
    """Build a lookup {path: ConfigEntry} from the real backend registry."""
    return {entry.path: entry for entry in SCHEMA_REGISTRY}


@pytest.fixture()
def generated_config_keys() -> list[str]:
    """Extract all configKey values from settingsRegistry.gen.ts."""
    assert (
        SETTINGS_REGISTRY_PATH.exists()
    ), f"settingsRegistry.gen.ts not found: {SETTINGS_REGISTRY_PATH}"
    return CONFIG_KEY_RE.findall(SETTINGS_REGISTRY_PATH.read_text(encoding="utf-8"))


class TestSettingRefSchemaFixtureDrift:
    """Every key referenced by frontend SettingRef must exist in the backend."""

    def test_fixture_keys_exist_in_registry(self, fixture_entries, registry_index):
        missing = [
            entry["path"] for entry in fixture_entries if entry["path"] not in registry_index
        ]
        assert not missing, (
            "Frontend SettingRef fixture references keys missing from backend "
            f"SCHEMA_REGISTRY: {missing}"
        )

    def test_fixture_types_match_registry(self, fixture_entries, registry_index):
        mismatches = []
        for entry in fixture_entries:
            backend = registry_index.get(entry["path"])
            if backend is None:
                continue  # covered by the key-presence test
            if backend.type != entry["type"]:
                mismatches.append(
                    f"{entry['path']}: fixture={entry['type']} backend={backend.type}"
                )
        assert (
            not mismatches
        ), f"Type mismatch between fixture and backend SCHEMA_REGISTRY: {mismatches}"


class TestSettingsRegistryGenConfigKeyDrift:
    """Every configKey in settingsRegistry.gen.ts must exist in the backend."""

    def test_generated_config_keys_found(self, generated_config_keys):
        assert generated_config_keys, "No configKey entries found in settingsRegistry.gen.ts"

    def test_all_config_keys_exist_in_schema_registry(self, generated_config_keys, registry_index):
        missing = [key for key in generated_config_keys if key not in registry_index]
        assert not missing, (
            "settingsRegistry.gen.ts configKey(s) missing from backend "
            f"SCHEMA_REGISTRY — typo or backend rename? Missing: {missing}"
        )


def _ts_const(source: str, name: str) -> str:
    """Read one exported TypeScript string literal."""
    match = re.search(TS_CONST_RE.format(name=name), source)
    assert match, f"{name} not found as a string constant in decisionsPreview.ts"
    return match.group(1)


class TestDecisionsSettingCrossLayer:
    """Keep the extracted literal, write path and backend schema in agreement."""

    def test_enabled_and_bucket_exist_in_schema_registry(self, registry_index):
        enabled = registry_index.get("decisions.enabled")
        bucket = registry_index.get("decisions.bucket")
        assert enabled is not None, "decisions.enabled missing from SCHEMA_REGISTRY"
        assert bucket is not None, "decisions.bucket missing from SCHEMA_REGISTRY"
        assert enabled.type == "boolean", f"decisions.enabled is {enabled.type}"
        assert bucket.type == "integer", f"decisions.bucket is {bucket.type}"

    def test_frontend_constants_match_the_schema_paths(self, registry_index):
        source = DECISIONS_READER_PATH.read_text(encoding="utf-8")
        for const_name, expected in (
            ("DECISIONS_ENABLED_PATH", "decisions.enabled"),
            ("DECISIONS_BUCKET_PATH", "decisions.bucket"),
        ):
            value = _ts_const(source, const_name)
            assert value == expected, f"{const_name} is {value!r}, expected {expected!r}"
            assert value in registry_index, f"{const_name} names no SCHEMA_REGISTRY path"

    def test_the_toggles_literal_config_key_matches_the_constant(self):
        reader = DECISIONS_READER_PATH.read_text(encoding="utf-8")
        card = FEATURE_PREVIEWS_PATH.read_text(encoding="utf-8")
        enabled_path = _ts_const(reader, "DECISIONS_ENABLED_PATH")
        assert f'configKey="{enabled_path}"' in card, (
            "FeaturePreviewsSection.tsx must spell configKey as a literal equal to "
            f"DECISIONS_ENABLED_PATH ({enabled_path!r}); an identifier expression "
            "extracts as nothing and the registry entry loses its config key"
        )

    def test_the_frontend_live_point_is_a_point_the_backend_ships(self):
        from kiro_crew.decisions.gate import DECISION_POINT_NAMES

        point = _ts_const(DECISIONS_READER_PATH.read_text(encoding="utf-8"), "DECISIONS_LIVE_POINT")
        assert (
            point,
        ) == DECISION_POINT_NAMES, (
            f"the card's point {point!r} differs from backend point names {DECISION_POINT_NAMES}"
        )


def _scan_backend_source_for_literal(name: str) -> bool:
    """Search backend Python files for a literal environment-variable name."""
    for dirpath, _dirs, files in os.walk(BACKEND_SRC_ROOT):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            if name in (Path(dirpath) / fname).read_text(encoding="utf-8"):
                return True
    return False


@pytest.fixture()
def env_vars_fixture() -> list[str]:
    assert ENV_VARS_FIXTURE_PATH.exists(), f"Env vars fixture not found: {ENV_VARS_FIXTURE_PATH}"
    return json.loads(ENV_VARS_FIXTURE_PATH.read_text(encoding="utf-8"))


class TestSettingRefEnvVarsDrift:
    """Every listed environment name must exist in backend source."""

    def test_fixture_not_empty(self, env_vars_fixture):
        assert env_vars_fixture, "settingref-env-vars.json is empty"

    def test_all_env_vars_found_in_backend_source(self, env_vars_fixture):
        missing = [name for name in env_vars_fixture if not _scan_backend_source_for_literal(name)]
        assert not missing, (
            "settingref-env-vars.json lists env vars not found in "
            f"src/kiro_crew/**/*.py: {missing}. Remove stale names or fix their spelling."
        )
