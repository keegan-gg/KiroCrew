"""The dashboard's two writable decision settings, through the real handlers.

``decisions.enabled`` is the value that lets a decision point send conversation
state to a third party, so these run the ACTUAL aiohttp handlers against a temp
``config.json`` rather than asserting the allowlist table: what matters is what a
PATCH stores and what a GET hands back, not that a dict has a key.

Three properties:

* PATCH accepts only a real boolean for the enable, and only an in-range integer
  for the bucket, and what it stores is what the config parses back.
* ``provider.*`` is NOT writable here. The endpoint would let a caller choose
  where state is sent, and ``api_key`` reads back masked -- a PATCH beside a
  masked GET would let a caller overwrite a key it cannot read.
* GET masks ``provider.api_key``, which is what makes the Settings card safe to
  render.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.sections import DECISION_BUCKET_MAX, DECISION_BUCKET_MIN

_BASE_CONFIG = {
    "agents": {"kirocrew": {"kiro_agent": "kirocrew"}},
    "default_agent": "kirocrew",
}


def _app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_config, api_kirocrew_config_patch

    app = web.Application()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    app.router.add_get("/api/config/kirocrew", api_kirocrew_config)
    return app


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """A temp ``config.json`` both handlers read and the PATCH writes."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_BASE_CONFIG), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    return path


async def _patch(client, path, value):
    return await client.patch("/api/config/kirocrew", json={"path": path, "value": value})


def _stored(path):
    return json.loads(path.read_text(encoding="utf-8")).get("decisions", {})


class TestPatchEnabled:
    @pytest.mark.asyncio
    async def test_true_is_stored_and_parses_back_as_enabled(self, config_file):
        from kiro_crew.config.loader import KiroCrewConfig

        async with TestClient(TestServer(_app())) as client:
            assert (await _patch(client, "decisions.enabled", True)).status == 200
        assert _stored(config_file)["enabled"] is True
        assert KiroCrewConfig.load().decisions.enabled is True

    @pytest.mark.asyncio
    async def test_false_is_stored(self, config_file):
        async with TestClient(TestServer(_app())) as client:
            assert (await _patch(client, "decisions.enabled", False)).status == 200
        assert _stored(config_file)["enabled"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["true", "True", 1, 0, "yes", None, [], {}])
    async def test_only_a_real_boolean_is_accepted(self, config_file, value):
        """A truthy string stored here would be read back as a string, not an enable."""
        async with TestClient(TestServer(_app())) as client:
            resp = await _patch(client, "decisions.enabled", value)
            assert resp.status == 400
            assert "boolean" in (await resp.json())["error"]
        assert "enabled" not in _stored(config_file)


class TestPatchBucket:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [DECISION_BUCKET_MIN, 1, 37, DECISION_BUCKET_MAX])
    async def test_an_in_range_bucket_is_stored(self, config_file, value):
        from kiro_crew.config.loader import KiroCrewConfig

        async with TestClient(TestServer(_app())) as client:
            assert (await _patch(client, "decisions.bucket", value)).status == 200
        assert _stored(config_file)["bucket"] == value
        assert KiroCrewConfig.load().decisions.bucket == value

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [-1, DECISION_BUCKET_MAX + 1, 1000])
    async def test_an_out_of_range_bucket_is_refused_not_clamped(self, config_file, value):
        """The write gate refuses, so the stored value always reads as what is in force."""
        async with TestClient(TestServer(_app())) as client:
            resp = await _patch(client, "decisions.bucket", value)
            assert resp.status == 400
            expected = f"between {DECISION_BUCKET_MIN} and {DECISION_BUCKET_MAX}"
            assert expected in (await resp.json())["error"]
        assert "bucket" not in _stored(config_file)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["lots", None, [10]])
    async def test_a_non_integer_bucket_is_refused(self, config_file, value):
        async with TestClient(TestServer(_app())) as client:
            resp = await _patch(client, "decisions.bucket", value)
            assert resp.status == 400
            assert "integer" in (await resp.json())["error"]


class TestProviderIsNotWritable:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path,value",
        [
            ("decisions.provider.endpoint", "https://judge.example.invalid/v1"),
            ("decisions.provider.api_key", "literal-key"),
            ("decisions.provider.model", "jev-nightly"),
            ("decisions.provider.timeout_ms", 5000),
            ("decisions.provider", {"endpoint": "https://judge.example.invalid/v1"}),
        ],
    )
    async def test_the_provider_cannot_be_written_from_the_dashboard(
        self, config_file, path, value
    ):
        async with TestClient(TestServer(_app())) as client:
            resp = await _patch(client, path, value)
            assert resp.status == 400
            assert "not editable" in (await resp.json())["error"]
        assert _stored(config_file) == {}


class TestGet:
    @pytest.mark.asyncio
    async def test_the_api_key_reads_back_masked(self, config_file):
        from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

        secret = "sk-" + "realsecret0123456789"
        config_file.write_text(
            json.dumps(
                {
                    **_BASE_CONFIG,
                    "decisions": {"enabled": True, "provider": {"api_key": secret}},
                }
            ),
            encoding="utf-8",
        )
        async with TestClient(TestServer(_app())) as client:
            body = await (await client.get("/api/config/kirocrew")).json()
        assert body["decisions"]["provider"]["api_key"] == _SENSITIVE_MASK
        assert secret not in json.dumps(body)

    @pytest.mark.asyncio
    async def test_the_two_writable_values_read_back_verbatim(self, config_file):
        """Neither is sensitive, so the Settings card gets its initial state."""
        async with TestClient(TestServer(_app())) as client:
            await _patch(client, "decisions.enabled", True)
            await _patch(client, "decisions.bucket", 25)
            body = await (await client.get("/api/config/kirocrew")).json()
        assert body["decisions"]["enabled"] is True
        assert body["decisions"]["bucket"] == 25
