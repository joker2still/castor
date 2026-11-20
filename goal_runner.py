"""Goal runner — execute one durable goal to completion (or pause/fail).

Bridges the asyncio worker loop to the synchronous orchestrator. Each call
to :func:`run` corresponds to one ``goal`` claimed off the queue: load
the latest checkpoint, invoke the orchestrator inside the default thread
pool, then mark the goal done/paused/failed based on outcome.

Phase 1 of this module used a thin wrapper around the chat-style
``agent.run()``. Phase 2 (this version) delegates to :mod:`orchestrator`
which:

    - uses ``prompts/orchestrator.md`` instead of ``soul.py``
    - has access only to plan-management + lightweight tools
    - dispatches heavy work to subagents via ``dispatch_subagent``
      (Phase 2c)

The goal_runner itself doesn't know or care which orchestrator strategy
is in use — that's encapsulated in :func:`orchestrator.run_orchestrator`.

**Acceptance gate (added 2026-05-16).** After every orchestrator return,
we run :func:`goal_validators.run_validator` over every subtask's
``done_condition``. Failures inject a remediation block as a
``system_note`` and re-enter the orchestrator (up to
``MAX_GATE_ATTEMPTS`` rounds). Exhaustion → ``mark_goal_failed`` with
``error="acceptance_gate_exhausted: ..."``. This replaces the old
auto-skip backstop, which silently masked failed work.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import config
import db
import goal_validators
import logger
import orchestrator
from turn_context import TurnContext

_log = logger.get("goal_runner")


MAX_GATE_ATTEMPTS = 3


def _classify_provider_error(exc: Exception) -> str | None:
    """Return a short ``provider_*`` reason if ``exc`` looks transient.

    A goal that crashes because the LLM provider rejected the request
    (402 out of credits, 429 rate limit, 5xx upstream blip) should be
    marked ``paused`` so the user can top up / wait and resume from
    the latest checkpoint — NOT ``failed``, which is the terminal
    state for "the work itself didn't work."

    We pattern-match on:
      1. ``status_code`` attribute (openai / anthropic SDKs expose it)
      2. The numeric code embedded in the string repr (covers the
         OpenRouter-via-openai-compat case where the SDK wraps the
         upstream response and the user-facing error is a string
         like ``APIStatusError: Error code: 402 - {...}``).

    Returns one of ``provider_billing_exhausted`` / ``provider_rate_limited``
    / ``provider_unavailable`` / ``None`` (not a provider error).
    """
    import re as _re
    status: int | None = None
    for attr in ("status_code", "code", "status", "http_status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            status = v
            break
        if isinstance(v, str) and v.isdigit():
            status = int(v)
            break
    if status is None:
        m = _re.search(r"Error code:\s*(\d{3})\b", str(exc))
        if m:
            status = int(m.group(1))
    if status is None:
        return None
    if status == 402:
        return "provider_billing_exhausted"
    if status == 429:
        return "provider_rate_limited"
    if 500 <= status <= 599:
        return "provider_unavailable"
    return None


def _gate_max_attempts() -> int:
    """Resolve the gate cap from config, fall back to module default.

    ``config.get`` raises ``KeyError`` for unregistered settings. The
    callable is wrapped so a missing/typo'd key never breaks the
    runner — we just use the module default.
    """
    try:
        v = config.get("acceptance_gate_max_attempts")
        return max(1, int(v)) if v else MAX_GATE_ATTEMPTS
    except (KeyError, TypeError, ValueError):
        return MAX_GATE_ATTEMPTS


def _checkpoint_interval() -> int:
    """Rounds between checkpoints. Configurable via EDITABLE_SETTINGS."""
    try:
        v = config.get("checkpoint_round_interval")
        return max(1, int(v)) if v else 3
    except (TypeError, ValueError):
        return 3


async def run(goal_id: str, shutdown_event: asyncio.Event) -> None:
    """Run one goal until terminal status.

    Never raises — all errors are caught and recorded on the goal row so the
    worker poll loop can keep going.
    """
    goal = db.get_goal(goal_id)
    if not goal:
        _log.warning(f"goal {goal_id} not found, skipping")
        return

    if goal["status"] in db.GOAL_TERMINAL_STATUSES:
        _log.info(f"goal {goal_id} already in terminal status {goal['status']}, skipping")
        return

    checkpoint = db.load_latest_checkpoint(goal_id)
    start_round = (checkpoint["round_num"] + 1) if checkpoint else 0
    if checkpoint:
        _log.info(f"resuming {goal_id} from round {checkpoint['round_num']}")
    else:
        _log.info(f"starting {goal_id} fresh")
        db.log_goal_event(goal_id, "goal_started",
                          {"input_preview": goal["user_input"][:200]})


    abort_event, _watcher_task = _bridge_shutdown_to_threading(shutdown_event)


    goal_workspace_path = str(config.goal_workspace(goal_id))

    ctx = TurnContext(
        source=goal["source"],
        abort_event=abort_event,
        goal_id=goal_id,
        on_round_complete=_make_checkpoint_callback(goal_id, start_round),
        workspace_root=goal_workspace_path,
    )


    max_attempts = _gate_max_attempts()
    gate_attempt = 0
    system_notes: list[str] = []
    final_result: dict | None = None

    try:
        loop = asyncio.get_running_loop()
        while True:


            if shutdown_event.is_set():
                db.mark_goal_paused(goal_id, reason="worker_shutdown")
                return

            gate_attempt += 1
            try:
                final_result = await loop.run_in_executor(
                    None,
                    lambda notes=tuple(system_notes): orchestrator.run_orchestrator(
                        goal_id=goal_id, ctx=ctx, system_notes=list(notes),
                    ),
                )
            except asyncio.CancelledError:

                _log.info(f"goal {goal_id} cancelled during run; marking paused")
                db.mark_goal_paused(goal_id, reason="worker_cancelled")
                raise
            except Exception as e:


                kind = _classify_provider_error(e)
                if kind:


                    _PROVIDER_BACKOFF = {
                        "provider_billing_exhausted": 300,
                        "provider_rate_limited": 60,
                        "provider_unavailable": 30,
                    }
                    backoff = _PROVIDER_BACKOFF.get(kind, 60)
                    _log.warning(
                        f"goal {goal_id} hit transient provider error ({kind}); "
                        f"pausing for {backoff}s before retry: {e}"
                    )
                    db.mark_goal_paused(goal_id, reason=kind, retry_after_sec=backoff)
                    return
                _log.exception(f"goal {goal_id} crashed: {e}")
                db.mark_goal_failed(goal_id, error=f"{type(e).__name__}: {e}")
                return


            if shutdown_event.is_set():
                db.mark_goal_paused(goal_id, reason="worker_shutdown")
                return


            plan = db.get_goal_plan(goal_id) or {}
            subtasks = plan.get("subtasks", [])


            if not subtasks and not (goal.get("done_conditions") or []):
                if gate_attempt >= max_attempts:
                    db.mark_goal_failed(
                        goal_id,
                        error="no_plan_created: orchestrator returned without "
                              "creating any subtasks after multiple attempts",
                    )
                    return
                system_notes = [
                    "PLAN REQUIRED: You returned without creating a plan. "
                    "You MUST call goal_plan_set with at least one subtask "
                    "before writing a final answer. Break the user's request "
                    "into concrete steps and execute them."
                ]
                goal = db.get_goal(goal_id) or goal
                continue

            failures: list[tuple[str, str]] = []
            for st in subtasks:


                if st.get("status") == "skipped":
                    continue
                cond = st.get("done_condition")
                if not cond:


                    continue
                try:
                    passed, remediation = goal_validators.run_validator(
                        cond, workspace_root=Path(goal_workspace_path),
                    )
                except Exception as e:  # noqa: BLE001 — last-resort guard


                    passed = False
                    remediation = f"validator crashed: {type(e).__name__}: {e}"
                if not passed:
                    failures.append((st["id"], remediation))
                    try:
                        db.update_subtask(
                            goal_id, st["id"],
                            validation_passed=False,
                            last_validation_failure=remediation,
                        )
                    except Exception:
                        _log.exception(
                            f"failed to record validation failure on {st['id']}"
                        )
                else:

                    if not st.get("validation_passed"):
                        try:
                            db.update_subtask(
                                goal_id, st["id"],
                                validation_passed=True,
                            )
                        except Exception:
                            _log.exception(
                                f"failed to record validation pass on {st['id']}"
                            )


            goal_failures: list[tuple[str, str]] = []
            for i, gc in enumerate(goal.get("done_conditions") or []):
                try:
                    passed, remediation = goal_validators.run_validator(
                        gc, workspace_root=Path(goal_workspace_path),
                    )
                except Exception as e:  # noqa: BLE001 — last-resort guard
                    passed = False
                    remediation = f"validator crashed: {type(e).__name__}: {e}"
                if not passed:


                    label = f"goal:cond_{i + 1}"
                    goal_failures.append((label, remediation))

            if not failures and not goal_failures:

                break


            all_failures: list[tuple[str, str]] = list(failures) + list(goal_failures)


            try:
                db.log_goal_event(goal_id, "acceptance_gate_blocked", {
                    "attempt": gate_attempt,
                    "failure_count": len(all_failures),
                    "subtask_failure_count": len(failures),
                    "goal_failure_count": len(goal_failures),
                    "failures": [
                        {"subtask_id": sid, "remediation": rem[:300]}
                        for sid, rem in all_failures
                    ],
                })
            except Exception:
                _log.exception(
                    f"failed to log acceptance_gate_blocked for {goal_id}"
                )

            if gate_attempt >= max_attempts:
                db.mark_goal_failed(
                    goal_id,
                    error=(
                        f"acceptance_gate_exhausted: "
                        f"{len(failures)} subtask + {len(goal_failures)} goal-level "
                        f"condition(s) still failing after {max_attempts} attempts"
                    ),
                )
                return


            note_lines: list[str] = []
            if failures:
                note_lines.extend([
                    "ACCEPTANCE GATE — subtask conditions NOT met:",
                    (
                        "Address each subtask and re-run subtask_update with "
                        "status=completed once your work makes the validator pass."
                    ),
                ])
                for sid, rem in failures:
                    note_lines.append(f"  - {sid}: {rem}")
            if goal_failures:
                if note_lines:
                    note_lines.append("")
                note_lines.extend([
                    "ACCEPTANCE GATE — goal-level deliverables NOT met:",
                    (
                        "These are the deliverables the user EXPLICITLY asked for. "
                        "They are MANDATORY — the goal cannot finish without them. "
                        "If the current plan doesn't cover them, call goal_plan_set "
                        "again to add the missing subtask(s) with proper done_conditions, "
                        "then execute. Do NOT write a final reply telling the user "
                        "to finish manually."
                    ),
                ])
                for label, rem in goal_failures:
                    note_lines.append(f"  - {label}: {rem}")
            system_notes = ["\n".join(note_lines)]


            goal = db.get_goal(goal_id) or goal
    finally:


        if not _watcher_task.done():
            _watcher_task.cancel()
            try:
                await _watcher_task
            except (asyncio.CancelledError, Exception):
                pass


        try:
            import skills.browser as _bs
        except ImportError:


            pass
        else:
            try:
                _bs._close_session(goal_id)
            except Exception:
                _log.exception(f"failed to close browser session for {goal_id}")


        try:


            new_ids = db.auto_attach_workspace_outputs(
                goal_id, workspace_root=goal_workspace_path,
            )
            if new_ids:
                _log.info(
                    f"auto-attached {len(new_ids)} workspace file(s) "
                    f"as outputs for {goal_id}"
                )
        except Exception:
            _log.exception(f"workspace auto-attach failed for {goal_id}")


    reply = (
        final_result.get("reply") if isinstance(final_result, dict) else ""
    ) or ""
    db.mark_goal_done(goal_id, result=reply)


def _make_checkpoint_callback(goal_id: str, start_round: int):
    """Build the on_round_complete callback that persists every N rounds.

    The callback runs inside the agent_loop thread (the executor where
    agent.run is running). SQLite is thread-safe with per-thread connections
    (db._local.conn), so the write happens on its own connection.
    """
    interval = _checkpoint_interval()

    def _cb(round_num: int, messages: list[dict]) -> None:
        global_round = start_round + round_num
        if global_round <= 0 or (global_round % interval) != 0:
            return
        try:
            db.save_checkpoint(
                goal_id,
                global_round,
                subtask_index=-1,
                messages=messages,
                plan={},
                facts={},
            )
            db.log_goal_event(goal_id, "checkpoint_saved",
                              {"round": global_round, "messages": len(messages)})
        except Exception:
            _log.exception(f"checkpoint failed for {goal_id} round {global_round}")

    return _cb


def _bridge_shutdown_to_threading(
    shutdown_event: asyncio.Event,
) -> "tuple[threading.Event, asyncio.Task]":
    """Return ``(threading.Event, watcher_task)``.

    The threading.Event fires whenever ``shutdown_event`` is set, so the
    sync agent loop (which lives in a thread executor and only knows
    about ``threading.Event`` for aborts via ``shell`` / ``http_request``)
    can poll it. The watcher_task is returned so the caller can cancel
    it in a ``finally`` — without that, every goal that completes WITHOUT
    setting shutdown_event leaks a pending task. Asyncio surfaces those
    as warnings: "Task pending ... wait_for=<Future pending>".
    """
    evt = threading.Event()

    async def _watcher() -> None:
        try:
            await shutdown_event.wait()
        except asyncio.CancelledError:
            return
        evt.set()

    task = asyncio.create_task(_watcher())
    return evt, task
