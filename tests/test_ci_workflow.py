"""V4 P1/P3: the CI workflow is self-contained and credential-free.

These tests guard two regression classes discovered in the post-V3 audit:

- **Expression-context bugs.** A plain YAML parse passes, but GitHub Actions
  rejects the workflow before any job is scheduled (the V3 failure: ``runner``
  was used in job-level ``env``, where that context is not available).
- **Accidental ``.env`` reliance.** CI must not depend on a developer machine
  exporting a ``.env`` file — every value it needs must be set explicitly in
  the workflow.

``actionlint`` (wired into the workflow itself, V4 P37) is the authoritative
semantic linter; these tests encode the specific invariants that a bare YAML
parse cannot catch.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
ACCEPTANCE = ROOT / "scripts" / "acceptance.sh"


def _load_workflow() -> dict:
    with WORKFLOW.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_ci_workflow_sets_database_url_explicitly() -> None:
    """The tests job must set the DB URL itself, not read it from a ``.env``."""
    wf = _load_workflow()
    env = wf["jobs"]["tests"].get("env", {})
    assert "DATABASE_URL" in env, "alembic needs an explicit DATABASE_URL"
    assert "TEST_DATABASE_URL" in env, "pytest needs an explicit TEST_DATABASE_URL"
    # Both must point at the job's own throwaway service, not a developer host.
    assert env["DATABASE_URL"].startswith("postgresql+asyncpg://")
    assert "127.0.0.1:5432" in env["DATABASE_URL"]


def test_ci_workflow_no_runner_context_in_job_level_env() -> None:
    """The exact V3 bug: ``runner`` is not a valid context in job-level ``env``.

    It is valid only in *step*-level ``env`` (and ``$GITHUB_ENV``), so any
    ``${{ runner.* }}`` expression under a job-level ``env`` block would fail
    before GitHub schedules a single job.
    """
    wf = _load_workflow()
    for job_id, job in wf["jobs"].items():
        job_env = job.get("env")
        if not isinstance(job_env, dict):
            continue
        for key, value in job_env.items():
            assert "runner." not in str(value), (
                f"jobs.{job_id}.env.{key} uses the `runner` context, which is "
                "not available at job level — move it to a step-level env."
            )


def test_ci_workflow_no_dotenv_reliance() -> None:
    """Neither CI nor the acceptance script may depend on a ``.env`` file."""
    for path in (WORKFLOW, ACCEPTANCE):
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        assert "cp .env" not in lowered
        assert "source .env" not in lowered
        assert "set -a" not in lowered  # would auto-export a sourced .env
        assert ".env" not in lowered, (
            f"{path.name} references a .env file; CI must be self-contained."
        )
