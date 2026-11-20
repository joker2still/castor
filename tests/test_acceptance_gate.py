"""Acceptance-gate tests for ``goal_runner.run``.

Workstream C of ``docs/specs/2026-05-16-acceptance-gate.md``.

The gate runs every subtask's ``done_condition`` through
``goal_validators.run_validator`` after each orchestrator return.
Failures inject a remediation note and re-enter the orchestrator;
exhaustion of ``MAX_GATE_ATTEMPTS`` marks the goal failed.

The orchestrator is mocked everywhere — these tests exercise the gate
control flow, not LLM behaviour. ``goal_validators.run_validator`` is
also monkeypatched per-test so we can deterministically drive pass /
fail transitions.

``db.update_subtask`` is monkeypatched everywhere we need to assert
``validation_passed`` / ``last_validation_failure`` kwargs. Workstream B
adds those kwargs to the real implementation; until that merges, the
runner's call-site is exercised via the mock.
"""
from __future__ import annotations

import asyncio
import json


def _make_goal_with_plan(db_mod, subtasks_with_conds: list[dict]) -> str:
    """Create a goal + plan, then graft a ``done_condition`` onto each
    stored subtask (since the current ``set_goal_plan`` doesn't carry
    them — workstream B's job).

    Each entry: ``{"title": ..., "done_condition": {...}}``.
    Returns the goal id.
    """
    goal_id = db_mod.create_goal(user_input="x", source="cli")
    db_mod.set_goal_plan(
        goal_id,
        [{"title": st["title"], "description": ""} for st in subtasks_with_conds],
    )


    conn = db_mod._get_conn()
    row = conn.execute("SELECT plan FROM goals WHERE id=?", (goal_id,)).fetchone()
    plan = json.loads(row[0])
    for st, in_st in zip(plan["subtasks"], subtasks_with_conds, strict=True):
        st["done_condition"] = in_st["done_condition"]
    conn.execute("UPDATE goals SET plan=? WHERE id=?", (json.dumps(plan), goal_id))
    conn.commit()
    return goal_id


def _patch_update_subtask_capture(db_mod, monkeypatch):
    """Replace ``db.update_subtask`` with a stub that records the kwargs
    used by the gate (so we can assert on them) and still writes
    ``validation_passed`` / ``last_validation_failure`` into the plan JSON
    so subsequent code-paths can read them back.

    Returns the list of recorded calls.
    """
    calls: list[dict] = []
    real = db_mod.update_subtask

    def _spy(goal_id, subtask_id, **kwargs):


        calls.append({"goal_id": goal_id, "subtask_id": subtask_id, **kwargs})


        wsb_passed = kwargs.pop("validation_passed", None)
        wsb_failure = kwargs.pop("last_validation_failure", None)
        if kwargs:
            real(goal_id, subtask_id, **kwargs)
        if wsb_passed is not None or wsb_failure is not None:
            conn = db_mod._get_conn()
            row = conn.execute(
                "SELECT plan FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
            if not row or not row[0]:
                return None
            plan = json.loads(row[0])
            for st in plan.get("subtasks", []):
                if st["id"] != subtask_id:
                    continue
                if wsb_passed is not None:
                    st["validation_passed"] = wsb_passed
                if wsb_failure is not None:
                    st["last_validation_failure"] = wsb_failure
                break
            conn.execute(
                "UPDATE goals SET plan=? WHERE id=?",
                (json.dumps(plan), goal_id),
            )
            conn.commit()

        return db_mod.get_goal_plan(goal_id)

    monkeypatch.setattr(db_mod, "update_subtask", _spy)
    return calls


def _run_goal(goal_runner_mod, goal_id: str) -> None:
    """Drive ``goal_runner.run`` to completion with a fresh asyncio event."""

    async def _go():
        shutdown = asyncio.Event()
        await goal_runner_mod.run(goal_id, shutdown)

    asyncio.run(_go())


def test_gate_passes_first_attempt_marks_goal_done(
    qwe_temp_data_dir, monkeypatch
):
    """When every done_condition validates True on the first attempt, the
    runner marks the goal done with the orchestrator's reply — exactly as
    in the old code path."""
    import db
    import goal_runner
    import goal_validators
    import orchestrator

    goal_id = _make_goal_with_plan(db, [
        {"title": "A", "done_condition": {"kind": "files_exist", "spec": {"paths": ["a.md"]}}},
        {"title": "B", "done_condition": {"kind": "files_exist", "spec": {"paths": ["b.md"]}}},
    ])
    capture = _patch_update_subtask_capture(db, monkeypatch)

    monkeypatch.setattr(goal_validators, "run_validator",
                        lambda c, workspace_root=None: (True, ""))
    orch_calls: list = []

    def _fake_orch(**kw):
        orch_calls.append(kw)
        return {"reply": "all done", "rounds": 1, "tools_used": [],
                "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(orchestrator, "run_orchestrator", _fake_orch)

    _run_goal(goal_runner, goal_id)


    assert len(orch_calls) == 1
    assert orch_calls[0].get("system_notes") == []

    g = db.get_goal(goal_id)
    assert g["status"] == "done"
    assert g["result"] == "all done"


    passed_ids = sorted(
        c["subtask_id"] for c in capture if c.get("validation_passed") is True
    )
    assert passed_ids == ["st_1", "st_2"]


def test_gate_recovers_on_second_attempt(qwe_temp_data_dir, monkeypatch):
    """Gate fails the first time, orchestrator "fixes" the issue on the
    second invocation, gate passes, goal marked done.

    Demonstrates the remediation re-entry loop end-to-end:
    1. Round 1: validator reports failure → system_note injected.
    2. Orchestrator runs again WITH the note in its system_notes kwarg.
    3. Round 2: validator reports pass → break out of loop.
    """
    import db
    import goal_runner
    import goal_validators
    import orchestrator

    goal_id = _make_goal_with_plan(db, [
        {"title": "A", "done_condition": {"kind": "files_exist", "spec": {"paths": ["a.md"]}}},
    ])
    _patch_update_subtask_capture(db, monkeypatch)


    invocations = {"orch": 0}

    def _fake_orch(**kw):
        invocations["orch"] += 1
        return {"reply": f"round {invocations['orch']} reply",
                "rounds": 1, "tools_used": [], "cost_usd": 0.0,
                "prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(orchestrator, "run_orchestrator", _fake_orch)

    def _validator(criterion, workspace_root=None):

        if invocations["orch"] >= 2:
            return True, ""
        return False, "Expected file 'a.md' to exist, but it does not. Create it."

    monkeypatch.setattr(goal_validators, "run_validator", _validator)

    orch_calls: list = []
    real_orch = orchestrator.run_orchestrator

    def _capturing_orch(**kw):
        orch_calls.append({k: v for k, v in kw.items() if k != "ctx"})
        return real_orch(**kw)

    monkeypatch.setattr(orchestrator, "run_orchestrator", _capturing_orch)

    _run_goal(goal_runner, goal_id)


    assert invocations["orch"] == 2


    assert orch_calls[0]["system_notes"] == []
    assert len(orch_calls[1]["system_notes"]) == 1
    note = orch_calls[1]["system_notes"][0]
    assert "ACCEPTANCE GATE" in note
    assert "st_1" in note
    assert "Expected file 'a.md'" in note


    g = db.get_goal(goal_id)
    assert g["status"] == "done"

    assert g["result"] == "round 2 reply"


    events = db.get_goal_events(goal_id)
    blocked = [e for e in events if e["event_type"] == "acceptance_gate_blocked"]
    assert len(blocked) == 1
    assert blocked[0]["payload"]["attempt"] == 1
    assert blocked[0]["payload"]["failure_count"] == 1


def test_gate_exhausts_attempts_marks_goal_failed(
    qwe_temp_data_dir, monkeypatch
):
    """Validator never passes → after MAX_GATE_ATTEMPTS attempts the goal
    is marked failed with ``acceptance_gate_exhausted`` in the error
    message."""
    import db
    import goal_runner
    import goal_validators
    import orchestrator


    monkeypatch.setattr(goal_runner, "_gate_max_attempts", lambda: 3)

    goal_id = _make_goal_with_plan(db, [
        {"title": "A", "done_condition": {"kind": "files_exist", "spec": {"paths": ["a.md"]}}},
    ])
    _patch_update_subtask_capture(db, monkeypatch)

    monkeypatch.setattr(
        goal_validators, "run_validator",
        lambda c, workspace_root=None: (False, "stuck: file missing"),
    )

    orch_count = {"n": 0}

    def _fake_orch(**kw):
        orch_count["n"] += 1
        return {"reply": "tried", "rounds": 1, "tools_used": [],
                "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(orchestrator, "run_orchestrator", _fake_orch)

    _run_goal(goal_runner, goal_id)


    assert orch_count["n"] == 3

    g = db.get_goal(goal_id)
    assert g["status"] == "failed"
    assert "acceptance_gate_exhausted" in g["error"]

    assert g["result"] is None or g["result"] == ""


def test_gate_logs_event_per_block(qwe_temp_data_dir, monkeypatch):
    """``acceptance_gate_blocked`` is logged once per failed gate attempt,
    each with attempt number + failure count + first 300 chars of each
    remediation."""
    import db
    import goal_runner
    import goal_validators
    import orchestrator

    monkeypatch.setattr(goal_runner, "_gate_max_attempts", lambda: 3)

    goal_id = _make_goal_with_plan(db, [
        {"title": "A", "done_condition": {"kind": "files_exist", "spec": {"paths": ["a.md"]}}},
        {"title": "B", "done_condition": {"kind": "files_exist", "spec": {"paths": ["b.md"]}}},
    ])
    _patch_update_subtask_capture(db, monkeypatch)

    monkeypatch.setattr(
        goal_validators, "run_validator",
        lambda c, workspace_root=None: (False, "remediate me"),
    )

    monkeypatch.setattr(orchestrator, "run_orchestrator", lambda **kw: {
        "reply": "tried", "rounds": 1, "tools_used": [],
        "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
    })

    _run_goal(goal_runner, goal_id)


    events = db.get_goal_events(goal_id)
    blocked = [e for e in events if e["event_type"] == "acceptance_gate_blocked"]
    assert len(blocked) == 3

    for i, e in enumerate(blocked, start=1):
        assert e["payload"]["attempt"] == i
        assert e["payload"]["failure_count"] == 2

        ids = sorted(f["subtask_id"] for f in e["payload"]["failures"])
        assert ids == ["st_1", "st_2"]
        for f in e["payload"]["failures"]:
            assert f["remediation"] == "remediate me"


def test_gate_writes_validation_passed_flag(qwe_temp_data_dir, monkeypatch):
    """After the gate runs, every subtask carries the right
    ``validation_passed`` flag — True for passing, False for failing — and
    failing subtasks also carry ``last_validation_failure``."""
    import db
    import goal_runner
    import goal_validators
    import orchestrator

    monkeypatch.setattr(goal_runner, "_gate_max_attempts", lambda: 1)

    goal_id = _make_goal_with_plan(db, [
        {"title": "A", "done_condition": {"kind": "files_exist", "spec": {"paths": ["a.md"]}}},
        {"title": "B", "done_condition": {"kind": "files_exist", "spec": {"paths": ["b.md"]}}},
    ])
    capture = _patch_update_subtask_capture(db, monkeypatch)


    def _validator(criterion, workspace_root=None):
        path = criterion["spec"]["paths"][0]
        if path == "a.md":
            return True, ""
        return False, "Expected file 'b.md' to exist, but it does not."

    monkeypatch.setattr(goal_validators, "run_validator", _validator)
    monkeypatch.setattr(orchestrator, "run_orchestrator", lambda **kw: {
        "reply": "tried", "rounds": 1, "tools_used": [],
        "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
    })

    _run_goal(goal_runner, goal_id)


    plan = db.get_goal_plan(goal_id)
    by_id = {st["id"]: st for st in plan["subtasks"]}

    assert by_id["st_1"].get("validation_passed") is True

    assert by_id["st_1"].get("last_validation_failure") in (None, "")

    assert by_id["st_2"].get("validation_passed") is False
    assert "b.md" in (by_id["st_2"].get("last_validation_failure") or "")


    pass_calls = [c for c in capture if c.get("validation_passed") is True]
    fail_calls = [c for c in capture if c.get("validation_passed") is False]
    assert [c["subtask_id"] for c in pass_calls] == ["st_1"]
    assert [c["subtask_id"] for c in fail_calls] == ["st_2"]
    assert "b.md" in fail_calls[0]["last_validation_failure"]


def test_gate_legacy_callers_without_done_condition_get_default(
    qwe_temp_data_dir, monkeypatch
):
    """Legacy / internal callers that pass plans without an explicit
    ``done_condition`` get a ``shell_returns_zero {cmd: 'true'}`` default
    from ``db.set_goal_plan`` (back-compat). The gate runs the validator,
    it passes, the goal is marked done.

    This documents the post-merge behavior: the LLM-facing
    ``tools.goal_plan_set`` HARD-REQUIRES done_condition, but the lower-
    level ``db.set_goal_plan`` accepts plans without it and inserts the
    safe default so older code paths still flow through cleanly.
    """
    import db
    import goal_runner
    import goal_validators
    import orchestrator


    goal_id = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(goal_id, [
        {"title": "A", "description": ""},
        {"title": "B", "description": ""},
    ])


    plan = db.get_goal_plan(goal_id)
    for st in plan["subtasks"]:
        assert st["done_condition"]["kind"] == "shell_returns_zero"


    call_count = {"n": 0}

    def _validator(criterion, workspace_root=None):
        call_count["n"] += 1
        return True, ""

    monkeypatch.setattr(goal_validators, "run_validator", _validator)
    monkeypatch.setattr(orchestrator, "run_orchestrator", lambda **kw: {
        "reply": "done", "rounds": 1, "tools_used": [],
        "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
    })

    _run_goal(goal_runner, goal_id)


    assert call_count["n"] == 2
    g = db.get_goal(goal_id)
    assert g["status"] == "done"


def test_gate_shutdown_mid_retry_pauses_goal(qwe_temp_data_dir, monkeypatch):
    """If ``shutdown_event`` fires while the gate is mid-retry, the runner
    marks the goal paused (``reason=worker_shutdown``) and stops.

    Mechanism: orchestrator's first run sets the shutdown event itself,
    then returns — the runner's post-run shutdown check kicks in.
    """
    import db
    import goal_runner
    import goal_validators
    import orchestrator

    monkeypatch.setattr(goal_runner, "_gate_max_attempts", lambda: 5)

    goal_id = _make_goal_with_plan(db, [
        {"title": "A", "done_condition": {"kind": "files_exist", "spec": {"paths": ["a.md"]}}},
    ])
    _patch_update_subtask_capture(db, monkeypatch)


    monkeypatch.setattr(
        goal_validators, "run_validator",
        lambda c, workspace_root=None: (False, "still missing"),
    )

    shutdown_evt: dict = {}

    def _fake_orch(**kw):


        if shutdown_evt.get("evt") is not None:
            shutdown_evt["evt"].set()
        return {"reply": "tried", "rounds": 1, "tools_used": [],
                "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(orchestrator, "run_orchestrator", _fake_orch)

    async def _go():
        shutdown = asyncio.Event()
        shutdown_evt["evt"] = shutdown
        await goal_runner.run(goal_id, shutdown)

    asyncio.run(_go())

    g = db.get_goal(goal_id)
    assert g["status"] == "paused"


def test_gate_orchestrator_exception_marks_goal_failed(
    qwe_temp_data_dir, monkeypatch
):
    """A raised exception inside the orchestrator surfaces as
    ``mark_goal_failed`` — same behaviour as pre-gate. The gate doesn't
    swallow orchestrator crashes."""
    import db
    import goal_runner
    import orchestrator

    goal_id = _make_goal_with_plan(db, [
        {"title": "A", "done_condition": {"kind": "files_exist", "spec": {"paths": ["a.md"]}}},
    ])
    _patch_update_subtask_capture(db, monkeypatch)

    def _crash(**kw):
        raise RuntimeError("LM Studio went away")

    monkeypatch.setattr(orchestrator, "run_orchestrator", _crash)

    _run_goal(goal_runner, goal_id)

    g = db.get_goal(goal_id)
    assert g["status"] == "failed"
    assert "RuntimeError" in g["error"]
    assert "LM Studio" in g["error"]
