from __future__ import annotations

import os

import pytest

from taiga_client import TaigaAPIError, TaigaClient


def test_init_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAIGA_BASE_URL", raising=False)
    monkeypatch.delenv("TAIGA_AUTH_TOKEN", raising=False)
    with pytest.raises(TaigaAPIError, match="TAIGA_BASE_URL"):
        TaigaClient()


@pytest.mark.asyncio
async def test_init_token_mode_omits_username_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAIGA_BASE_URL", "https://taiga.example.com/api/v1")
    monkeypatch.setenv("TAIGA_AUTH_TOKEN", "secret-token")
    monkeypatch.delenv("TAIGA_USERNAME", raising=False)
    monkeypatch.delenv("TAIGA_PASSWORD", raising=False)
    c = TaigaClient()
    try:
        assert c._static_bearer == "secret-token"  # noqa: SLF001
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_authenticate_uses_bearer_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAIGA_BASE_URL", "https://taiga.example.com")
    monkeypatch.setenv("TAIGA_AUTH_TOKEN", "Bearer  myapp")
    monkeypatch.delenv("TAIGA_USERNAME", raising=False)
    monkeypatch.delenv("TAIGA_PASSWORD", raising=False)

    c = TaigaClient()
    assert c._static_bearer == "myapp"  # noqa: SLF001
    await c.authenticate()
    assert c._client.headers.get("Authorization") == "Bearer myapp"  # noqa: SLF001
    await c.close()


def test_init_password_mode_requires_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAIGA_BASE_URL", "https://taiga.example.com")
    monkeypatch.delenv("TAIGA_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TAIGA_USERNAME", raising=False)
    with pytest.raises(TaigaAPIError, match="TAIGA_USERNAME"):
        TaigaClient()
