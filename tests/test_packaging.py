"""P43 packaging guard: production must build from the lockfile.

These tests are cheap file-level checks (no Docker build) that fail if the
Dockerfile silently regresses to resolving from ``pyproject.toml`` ranges
(``pip install .``) or if the lockfile it depends on is missing. The
authoritative in-sync check is ``uv lock --check`` in ``scripts/acceptance.sh``.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_uv_lockfile_is_present() -> None:
    """The Dockerfile's ``uv sync --frozen`` is only meaningful if uv.lock exists."""
    assert (REPO_ROOT / "uv.lock").is_file()


def test_dockerfile_installs_from_the_lockfile() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    # Production installs are driven by uv and the lockfile, not pip ranges.
    assert "uv sync --frozen" in dockerfile
    # No dev/test tooling ships in the production image.
    assert "--no-dev" in dockerfile

    # A regression to range-based pip resolution would bypass the lockfile.
    assert "pip install ." not in dockerfile
    assert "pip install -e" not in dockerfile
    assert "pip install -r" not in dockerfile
