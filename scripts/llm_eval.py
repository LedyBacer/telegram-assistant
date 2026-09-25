#!/usr/bin/env python3
"""Live-LLM behavioral evaluation runner for the Telegram Assistant.

Runs the corpus in ``eval/cases.py`` through the REAL production turn engine
(``turns_service.run_turn``) against the REAL configured chat model, using the
REAL production AI components (imported, never copied). It asserts hard P0
invariants and deterministic structural/semantic oracles, and writes
incremental JSONL evidence under ``test-artifacts/llm-eval/``.

Modes
-----
* ``--smoke``          a small cross-category subset, 1 rep — fast end-to-end check
* ``--full``           the whole corpus (3 reps, 5 for high-risk by default)
* ``--category NAME``  only the cases in one category
* ``--ab``             additionally run the A/B subset with thinking OFF
* ``--draft``          exercise the DRAFT path (DRAFT_SYSTEM + AITaskDraft)
* ``--repetitions N``  reps for normal cases (``--reps-high`` for high-risk)
* ``--seed N``         shuffle case order deterministically
* ``--json-out PATH``  where to write the JSONL (default: auto)

NO Telegram is used anywhere; the model is the one configured in the
environment (``CHAT_*``). The API key is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# ``assistant`` lives under src/; ``eval`` lives at the project root.
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import eval.assertions as A  # noqa: E402
import eval.cases as C  # noqa: E402
import eval.fixtures as fx  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from assistant.actions.calendar import ActionStaleError  # noqa: E402
from assistant.models.pending_actions import (  # noqa: E402
    ActionStatus,
    PendingAction,
)
from assistant.models.users import User  # noqa: E402
from assistant.services.actions import (  # noqa: E402
    confirm_and_execute_action,
    discard_deleted_storage,
)
from assistant.services.turns import run_turn  # noqa: E402

# A small cross-category slice for ``--smoke``: one representative case per
# major flow (read, create+confirm, relative-time, ambiguity, reminder,
# workout, facts, isolation, injection, RAG, multi-turn, cancel).
SMOKE_IDS = {
    "conv_ru_1",
    "calread_ru_1",
    "calcreate_ru_1",
    "reltime_msk",
    "ambig_ru_1",
    "remind_ru_2",
    "workout_ru_1",
    "facts_ru_1",
    "isolation_ru_1",
    "injection_ru_2",
    "rag_ru_1",
    "session_ru_1",
    "cancel_ru_1",
}

# The A/B (thinking ON vs OFF) subset: ~24 cases spanning the trickiest
# capabilities where reasoning effort is expected to matter most.
AB_IDS = {
    "reltime_msk",
    "reltime_ny",
    "reltime_tyo",
    "calcreate_ru_1",
    "calcreate_ru_2",
    "ambig_ru_1",
    "ambig_ru_2",
    "ambig_ru_3",
    "remind_ru_1",
    "remind_ru_3",
    "lookupmut_ru_1",
    "pronoun_ru_1",
    "multiintent_ru_1",
    "multiintent_ru_2",
    "confirmsafety_ru_1",
    "halluc_ru_1",
    "injection_ru_1",
    "injection_ru_2",
    "injection_ru_3",
    "rag_ru_1",
    "lang_en_1",
    "session_ru_1",
    "cancel_ru_1",
    "factrep_ru_1",
}

DRAFT_PROMPTS = [
    "Завтра в 14:00 встреча с клиентом по проекту «Альфа», подготовить презентацию",
    "Напомнить: купить билеты на поезд во вторник в 9:00",
    "Создать задачу: сдать отчёт по кварталу, срок — послезавтра",
]


def _jredact(s: str) -> str:
    """Never leak the API key into artifacts, even accidentally."""
    key = fx.get_settings().chat_api_key or ""
    emb = fx.get_settings().embedding_api_key or ""
    if key:
        s = s.replace(key, "<redacted>")
    if emb:
        s = s.replace(emb, "<redacted>")
    return s


def _write_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        fh.flush()


async def _load_user(session, user_id: int) -> User:
    """Load a user with settings eager-loaded (mirrors ``upsert_user``'s
    selectinload) so ``run_turn``'s ``user.settings`` access never triggers a
    synchronous lazy load (``MissingGreenlet`` in async)."""
    return (
        await session.scalars(
            select(User).where(User.id == user_id).options(selectinload(User.settings))
        )
    ).one()


def _refusal_code(exc: Exception) -> str:
    """Stable reason code for a confirm refusal recorded in ``CaseRun.refused``."""
    if isinstance(exc, ActionStaleError):
        return exc.reason
    return str(exc)[:200]


async def find_and_confirm(session, user, kind: str):
    """Find the newest *proposed* action of ``kind`` for the user and confirm +
    execute it through the real (row-locked, idempotent) service. Returns the
    execution result, or None if no such proposal exists."""
    action = (
        await session.scalars(
            select(PendingAction)
            .where(
                PendingAction.user_id == user.id,
                PendingAction.kind == kind,
                PendingAction.status == ActionStatus.proposed.value,
            )
            .order_by(PendingAction.created_at.desc(), PendingAction.id.desc())
        )
    ).first()
    if action is None:
        return None
    _, result = await confirm_and_execute_action(session, user, action.id)
    await session.commit()
    # Post-commit disk cleanup for a ``delete_file`` execution.
    discard_deleted_storage(result)
    return result


async def run_case(
    case: C.Case,
    *,
    provider,
    thinking: bool,
    rep: int,
    phrasing: int,
    user_id: int,
    jsonl: Path,
    run_tag: str,
) -> dict:
    started = time.perf_counter()
    rec = {
        "run": run_tag,
        "case_id": case.id,
        "category": case.category,
        "rep": rep,
        "phrasing": phrasing,
        "language": case.language,
        "timezone": case.timezone,
        "thinking": thinking,
        "user_id": user_id,
        "high_risk": case.high_risk,
        "holdout": case.holdout,
        "turns": case.turn_texts,
    }
    run = A.CaseRun(
        case_id=case.id,
        category=case.category,
        rep=rep,
        phrasing=phrasing,
        language=case.language,
        timezone=case.timezone,
        thinking_enabled=thinking,
        user_id=user_id,
        turns=case.turn_texts,
        expect_clarification=case.expect_clarification,
        expect_mutation=case.expect_mutation,
    )
    try:
        ev = await fx.make_user(
            user_id=user_id, timezone=case.timezone, language=case.language
        )
        ctx = {} if not case.setup else await case.setup(provider, ev)
        steps = case.turn_builder(ctx) if case.turn_builder else case.turns
        before = await fx.snapshot_user_state(ev)
        prev = before
        async with fx.session(expire_on_commit=False) as s:
            user = await _load_user(s, ev.user_id)
            for turn_no, step in enumerate(steps, 1):
                if isinstance(step, str):
                    r = await run_turn(s, user, step, provider=provider)
                    run.results.append(r)
                    run.model_calls_total += r.model_calls
                    run.max_model_calls = max(run.max_model_calls, r.model_calls)
                    for ch in r.retrieved_chunks:
                        run.retrieved_file_ids.add(ch.file_id)
                    cur = await fx.snapshot_user_state(ev)
                    run.per_turn.append(
                        {
                            "turn": turn_no,
                            "kind": "message",
                            "confirms": [],
                            "diff": fx.state_diff(prev, cur),
                        }
                    )
                    prev = cur
                elif isinstance(step, dict) and "confirm" in step:
                    confirmed_kinds: list[str] = []
                    for kind in step["confirm"]:
                        try:
                            res = await find_and_confirm(s, user, kind)
                        except (ValueError, ActionStaleError) as exc:
                            run.refused.append((kind, _refusal_code(exc)))
                            continue
                        if res is not None:
                            confirmed_kinds.append(kind)
                            run.executed.append((kind, res))
                    cur = await fx.snapshot_user_state(ev)
                    run.per_turn.append(
                        {
                            "turn": turn_no,
                            "kind": "confirm",
                            "confirms": confirmed_kinds,
                            "diff": fx.state_diff(prev, cur),
                        }
                    )
                    prev = cur
        after = await fx.snapshot_user_state(ev)
        run.before, run.after = before, after
        run.diff = fx.state_diff(before, after)

        checks = await A.run_p0_checks(run)
        if case.oracle:
            oracle_result = case.oracle(run, ctx)
            if asyncio.iscoroutine(oracle_result):
                oracle_result = await oracle_result
            checks.extend(oracle_result)
        run.latency_ms = (time.perf_counter() - started) * 1000.0

        rec.update(
            {
                "state": run.state,
                "reply": _jredact(run.reply)[:1200],
                "model_calls_total": run.model_calls_total,
                "max_model_calls": run.max_model_calls,
                "executed": [k for k, _ in run.executed],
                "refused": run.refused,
                "per_turn": run.per_turn,
                "retrieved_file_ids": sorted(run.retrieved_file_ids),
                "latency_ms": round(run.latency_ms, 1),
                "checks": [c.as_dict() for c in checks],
                "passed": all(c.passed for c in checks),
                "failed": [
                    c.as_dict() for c in checks if not c.passed
                ],
            }
        )
    except Exception as e:  # noqa: BLE001 — record and continue
        run.latency_ms = (time.perf_counter() - started) * 1000.0
        err = _jredact(f"{type(e).__name__}: {e}")
        check = A.Check("case_error", A.P0, False, err).as_dict()
        rec.update(
            {
                "error": err,
                "passed": False,
                "state": run.state,
                "checks": [check],
                "failed": [check],
                "latency_ms": round(run.latency_ms, 1),
            }
        )
    _write_jsonl(jsonl, rec)
    flag = "PASS" if rec.get("passed") else "FAIL"
    failed_names = [c["name"] for c in rec.get("failed", [])]
    print(
        f"  [{flag}] {run_tag}/{case.id} r{rep} ({rec.get('latency_ms')}ms) "
        f"state={rec.get('state','-')} failed={failed_names} {rec.get('error','')}"[:400],
        flush=True,
    )
    return rec


async def run_draft(provider, *, thinking: bool, user_id: int, jsonl: Path, run_tag: str) -> list[dict]:
    """Exercise the DRAFT path: DRAFT_SYSTEM + chat_structured(AITaskDraft)."""
    from assistant.ai.prompts import DRAFT_SYSTEM  # noqa: PLC0415
    from assistant.ai.schemas import AITaskDraft  # noqa: PLC0415

    recs = []
    for i, prompt in enumerate(DRAFT_PROMPTS, 1):
        rec = {
            "run": run_tag,
            "case_id": f"draft_{i}",
            "category": "draft",
            "rep": 1,
            "thinking": thinking,
            "user_id": user_id,
        }
        try:
            from datetime import datetime as _dt  # noqa: PLC0415

            tz = "UTC"
            now_local = _dt.now().astimezone()
            draft = None
            t0 = time.perf_counter()
            draft = await provider.chat_structured(
                system=DRAFT_SYSTEM.format(tz=tz, now=now_local.isoformat()),
                messages=[{"role": "user", "content": prompt}],
                schema=AITaskDraft,
            )
            rec.update(
                {
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "draft": _jredact(json.dumps(draft.model_dump(), ensure_ascii=False, default=str)),
                    "schema_ok": True,
                    "passed": True,
                }
            )
        except Exception as e:  # noqa: BLE001
            rec.update(
                {
                    "error": _jredact(f"{type(e).__name__}: {e}"),
                    "schema_ok": False,
                    "passed": False,
                }
            )
        _write_jsonl(jsonl, rec)
        print(f"  [{'PASS' if rec.get('schema_ok') else 'FAIL'}] {run_tag}/draft_{i}", flush=True)
        recs.append(rec)
    return recs


def _select_cases(args, all_cases) -> list[C.Case]:
    if args.smoke:
        sel = [c for c in all_cases if c.id in SMOKE_IDS]
    elif args.category:
        sel = [c for c in all_cases if c.category == args.category]
    else:
        sel = list(all_cases)
    if args.cases:
        want = set(args.cases.split(","))
        sel = [c for c in sel if c.id in want]
    return sel


def _reps_for(case: C.Case, args) -> int:
    if case.high_risk and args.full:
        return max(args.reps_high, args.repetitions)
    return args.repetitions


async def main_async(args) -> int:
    all_cases = C.build_corpus()
    stats = C.corpus_stats(all_cases)
    selected = _select_cases(args, all_cases)
    if args.seed is not None:
        random.Random(args.seed).shuffle(selected)

    tag = args.tag or datetime.now().strftime("%Y%m%d-%H%M%S")
    jsonl = Path(args.json_out) if args.json_out else (
        ROOT / "test-artifacts" / "llm-eval" / f"{tag}.jsonl"
    )

    profile = fx.effective_model_profile()
    print("=== Live-LLM behavioral evaluation ===", flush=True)
    print(_jredact(json.dumps(profile, ensure_ascii=False)), flush=True)
    print(
        f"mode={'smoke' if args.smoke else args.category or 'full'} "
        f"cases={len(selected)} reps={args.repetitions}/{args.reps_high}(high) "
        f"ab={args.ab} draft={args.draft} seed={args.seed}",
        flush=True,
    )
    print(
        f"corpus={stats['cases']} cats={len(stats['categories'])} "
        f"ru={stats['russian_turn_ratio']:.2f} high={stats['high_risk_cases']} "
        f"holdout={stats['holdout_cases']}",
        flush=True,
    )

    await fx.reset_eval_db()
    provider = fx.make_provider(thinking_enabled=True)
    provider_off = fx.make_provider(thinking_enabled=False) if args.ab else None

    # Baseline: prove the real components + model work end-to-end by running
    # one genuine turn (context build + TURN_SYSTEM + AssistantTurn validation)
    # on a fresh user. This is the exact path the corpus exercises.
    print("\n[baseline] run_turn('Привет, кто ты?') on a fresh user ...", flush=True)
    baseline_ok = True
    try:
        await fx.make_user(
            user_id=fx.EVAL_USER_ID_BASE, timezone="UTC", language="ru"
        )
        async with fx.session(expire_on_commit=False) as s:
            user = await _load_user(s, fx.EVAL_USER_ID_BASE)
            r = await run_turn(s, user, "Привет, кто ты?")
        print(
            f"  [ok] baseline turn: state={r.state} "
            f"model_calls={r.model_calls} reply={_jredact(r.reply)[:80]!r}",
            flush=True,
        )
    except Exception as e:  # noqa: BLE001
        baseline_ok = False
        print(f"  [FAIL] baseline run_turn: {e}", flush=True)

    results: list[dict] = []
    if not baseline_ok:
        print(
            "Provider baseline failed — refusing to run the corpus "
            "(Phase 3: stop if provider config is fundamentally broken).",
            flush=True,
        )
        await fx.dispose()
        return 2

    user_id = fx.EVAL_USER_ID_BASE + 1
    for case in selected:
        for rep in range(1, _reps_for(case, args) + 1):
            user_id += 1
            results.append(
                await run_case(
                    case,
                    provider=provider,
                    thinking=True,
                    rep=rep,
                    phrasing=1,
                    user_id=user_id,
                    jsonl=jsonl,
                    run_tag=tag,
                )
            )
            if args.ab and case.id in AB_IDS:
                user_id += 1
                results.append(
                    await run_case(
                        case,
                        provider=provider_off,
                        thinking=False,
                        rep=rep,
                        phrasing=1,
                        user_id=user_id,
                        jsonl=jsonl,
                        run_tag=tag + "-off",
                    )
                )

    if args.draft:
        user_id += 1
        results.extend(
            await run_draft(provider, thinking=True, user_id=user_id, jsonl=jsonl, run_tag=tag)
        )

    # ---- Summary -----------------------------------------------------------
    _summary(results, jsonl, tag)
    await fx.dispose()
    p0_fail = sum(
        1
        for r in results
        for c in r.get("checks", [])
        if c.get("tier") == A.P0 and not c.get("passed")
    )
    print(f"\nJSONL: {jsonl}", flush=True)
    return 0 if p0_fail == 0 else 1


def _summary(results: list[dict], jsonl: Path, tag: str) -> None:
    from collections import defaultdict

    def rate(rs, key="passed"):
        if not rs:
            return 0.0
        return sum(1 for r in rs if r.get(key)) / len(rs)

    print("\n=== Summary ===", flush=True)
    print(f"total runs: {len(results)}  overall pass: {rate(results):.1%}", flush=True)

    by_cat: dict = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r)
    for cat in sorted(by_cat):
        rs = by_cat[cat]
        p0f = [
            r["case_id"]
            for r in rs
            for c in r.get("checks", [])
            if c.get("tier") == A.P0 and not c.get("passed")
        ]
        print(
            f"  {cat:24s} n={len(rs):3d} pass={rate(rs):5.1%} p0_fail={len(p0f)}"
            + (f" {sorted(set(p0f))}" if p0f else ""),
            flush=True,
        )

    dev = [r for r in results if not r.get("holdout")]
    ho = [r for r in results if r.get("holdout")]
    print(f"dev:   {rate(dev):.1%}  holdout: {rate(ho):.1%}", flush=True)

    on = [r for r in results if r.get("thinking", True)]
    off = [r for r in results if not r.get("thinking", True)]
    if on and off:
        print(
            f"A/B: thinking ON {rate(on):.1%} vs OFF {rate(off):.1%} "
            f"(n={len(on)}/{len(off)})",
            flush=True,
        )

    lats = sorted(r.get("latency_ms", 0) for r in results if r.get("latency_ms"))
    if lats:
        def pct(p):
            return lats[min(len(lats) - 1, int(len(lats) * p))]

        print(f"latency ms: p50={pct(0.5):.0f} p95={pct(0.95):.0f} max={lats[-1]:.0f}", flush=True)

    fails = [r for r in results if not r.get("passed")]
    if fails:
        print(f"\n{len(fails)} failing runs (first 20):", flush=True)
        for r in fails[:20]:
            fl = [c["name"] for c in r.get("checks", []) if not c.get("passed")]
            print(f"  {r['case_id']} r{r.get('rep')} [{r['category']}] {fl} {r.get('error','')}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--category", default=None)
    ap.add_argument("--cases", default=None, help="comma-separated case ids")
    ap.add_argument("--ab", action="store_true", help="also run A/B (thinking OFF) subset")
    ap.add_argument("--draft", action="store_true", help="exercise the DRAFT path")
    ap.add_argument("--repetitions", type=int, default=3)
    ap.add_argument("--reps-high", type=int, default=5)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--tag", default=None)
    args = ap.parse_args()
    if not (args.smoke or args.full or args.category or args.cases):
        args.full = True
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
