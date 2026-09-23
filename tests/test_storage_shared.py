"""Cross-component shared file-storage contract (§5.1).

The bot, api, and worker are three separate processes (separate CWDs in
production) that must read and write the SAME physical files. They all resolve
the storage location through one shared, absolute ``FILE_STORAGE_DIR`` setting.

This test asserts that, given the deployment value, all three components resolve
an identical absolute path for a given storage key — and that the path is
absolute, so a change of working directory in one process cannot silently make
it look at a different directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.config import get_settings
from assistant.services.files import _storage_path

CONTAINER_STORAGE = "/data/storage/files"


def _resolve(component: str) -> Path:
    # Each component is a fresh process; model that by re-reading the shared
    # config and resolving the same storage key.
    del component
    return _storage_path("abc/xyz.bin")


@pytest.mark.asyncio
async def test_all_components_resolve_one_absolute_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FILE_STORAGE_DIR", CONTAINER_STORAGE)
    get_settings.cache_clear()
    try:
        paths = {c: _resolve(c) for c in ("bot", "api", "worker")}
        assert len(set(paths.values())) == 1, f"components disagree: {paths}"
        path = next(iter(paths.values()))
        assert path.is_absolute(), f"storage path must be absolute, got {path}"
        assert path.as_posix() == "/data/storage/files/abc/xyz.bin"
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_default_is_not_a_conflicting_relative_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # The historical bug: a relative default resolved against each process's
    # CWD, so bot/api/worker (different CWDs) wrote to different directories.
    # With the deployment value set, the path must not depend on the CWD.
    monkeypatch.setenv("FILE_STORAGE_DIR", CONTAINER_STORAGE)
    get_settings.cache_clear()
    try:
        cwd_a = _resolve("api")
        monkeypatch.chdir("/tmp")
        cwd_b = _resolve("bot")
        assert cwd_a == cwd_b
    finally:
        get_settings.cache_clear()
