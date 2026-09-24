"""V4 P1/P3 + V5 §2: the CI workflow is self-contained and credential-free.

These tests guard regression classes discovered in the post-V3/V4 audits:

- **Expression-context bugs.** A plain YAML parse passes, but GitHub Actions
  rejects the workflow before any job is scheduled (the V3 failure: ``runner``
  was used in job-level ``env``, where that context is not available).
- **Accidental ``.env`` reliance.** CI must not depend on a developer machine
  exporting a ``.env`` file — every value it needs must be set explicitly in
  the workflow.
- **Docker Compose ``.env`` optionality.** Every runtime service that
  references ``.env`` must use ``required: false`` so the compose model is
  valid on a clean checkout (V5 §2.1).
- **Pinned official actionlint.** The workflow must use
  ``docker://rhysd/actionlint:1.7.12``, not the broken ``rsteube/actionlint``
  action (V5 §2.2).
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
ACCEPTANCE = ROOT / "scripts" / "acceptance.sh"
COMPOSE = ROOT / "docker-compose.yml"


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
        # Node's `process.env` global (the Playwright webServer env merge in the
        # E2E step) is not a dotenv file; strip it so the broad check below
        # targets an actual `.env` file reference, not that global.
        stripped = lowered.replace("process.env", "")
        assert ".env" not in stripped, (
            f"{path.name} references a .env file; CI must be self-contained."
        )


def test_compose_env_file_is_optional() -> None:
    """Every runtime service referencing ``.env`` must use ``required: false``.

    A clean GitHub checkout has no ``.env`` (it is in .gitignore), so the
    compose model must be valid without it (V5 §2.1).
    """
    with COMPOSE.open(encoding="utf-8") as fh:
        compose = yaml.safe_load(fh)
    for svc_name, svc in compose["services"].items():
        env_file = svc.get("env_file")
        if env_file is None:
            continue
        # Long syntax: list of {path, required} mappings.
        if isinstance(env_file, list):
            for entry in env_file:
                if isinstance(entry, dict) and "path" in entry:
                    assert entry.get("required") is False, (
                        f"services.{svc_name}: env_file path={entry['path']} "
                        "must have required: false"
                    )
                else:
                    raise AssertionError(
                        f"services.{svc_name}: env_file entry {entry!r} is not "
                        "the long form with path+required: false"
                    )
        else:
            # Short string form is NOT acceptable — it implies required.
            raise AssertionError(
                f"services.{svc_name}: env_file uses short form ({env_file!r}); "
                "must use long form with required: false"
            )


def test_actionlint_uses_pinned_official_image() -> None:
    """The workflow must use docker://rhysd/actionlint:1.7.12 (V5 §2.2).

    The old rsteube/actionlint@v3.5.0 action was broken; the official Docker
    image pinned to a concrete version is required.
    """
    wf = _load_workflow()
    lint_job = wf["jobs"]["lint"]
    found = False
    for step in lint_job["steps"]:
        uses = step.get("uses", "")
        if "actionlint" in uses:
            found = True
            assert "rsteube/actionlint" not in uses, (
                "old rsteube/actionlint reference must be removed"
            )
            assert uses == "docker://rhysd/actionlint:1.7.12", (
                f"expected docker://rhysd/actionlint:1.7.12, got {uses!r}"
            )
            assert step.get("with", {}).get("args") == "-color", (
                "actionlint step must pass -color"
            )
    assert found, "no actionlint step found in lint job"
