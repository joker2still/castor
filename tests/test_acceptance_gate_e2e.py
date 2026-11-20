"""End-to-end test for the acceptance gate.

Stitches together the three workstreams the gate is built from:

  A — goal_validators.run_validator runs against the real filesystem
  B — db.set_goal_plan stores done_condition, db.update_subtask honors it
  C — goal_runner re-enters the orchestrator with a remediation note on
      first failure, succeeds on the second attempt.

Scenario:
  1. Goal created with one subtask: "Create docs/report.md with a
     '## Findings' section". done_condition = regex_in_file checking
     the section header.
  2. First orchestrator pass: writes the file BUT without the heading
     (simulating premature completion / capitulation). Calls
     subtask_update("st_1", "completed").
  3. Gate fires. Validator reports failure (regex not found). Goal
     stays in flight; runner builds a remediation system_note and
     re-enters orchestrator with attempt=2.
  4. Second pass: orchestrator sees the remediation note in its message
     stream. We make the mock LLM "respond" by rewriting the file with
     the heading. Calls subtask_update("st_1", "completed") again.
  5. Gate re-runs. Validator passes. Goal marked done.

The mocked LLM is just a Python callable swapped in for
``orchestrator.run_orchestrator``; we drive plan/file state ourselves
based on the attempt count. This proves the GATE WIRING works — the
loop, the remediation injection, the second-attempt success path — not
that any particular LLM passes the gate.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import config
import db
import goal_runner
import orchestrator


def test_e2e_first_attempt_fails_second_attempt_passes(qwe_temp_data_dir, monkeypatch):

    goal_id = db.create_goal(
        user_input="Write docs/report.md with a Findings section",
        source="cli",
    )


    goal_workspace = config.goal_workspace(goal_id)
    report_path = goal_workspace / "report.md"
    if report_path.exists():
        report_path.unlink()
    db.set_goal_plan(goal_id, [{
        "title": "Author report.md with the Findings section",
        "description": "Write workspace/report.md including a '## Findings' heading",
        "done_condition": {
            "kind": "regex_in_file",
            "spec": {"path": "report.md", "pattern": r"^## Findings\b"},
        },
    }])


    attempt_log: list[dict] = []

    def _fake_run_orchestrator(*, goal_id, ctx, system_notes=None, **kw):
        attempt = len(attempt_log) + 1
        notes = list(system_notes or [])
        attempt_log.append({"attempt": attempt, "system_notes": notes})

        if attempt == 1:


            report_path.write_text(
                "# Report\n\n"
                "Some prose without the required findings heading.\n"
            )
            db.update_subtask(
                goal_id, "st_1", status="completed",
                result_summary="report written",
            )
            return {
                "reply": "Done — wrote report.md.",
                "rounds": 1, "tools_used": ["write_file", "subtask_update"],
                "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
            }


        assert notes, "second attempt must receive system_notes from the gate"
        note_text = "\n".join(notes)
        assert "ACCEPTANCE GATE" in note_text, (
            f"system_note didn't carry the gate header: {note_text[:200]!r}"
        )
        assert "st_1" in note_text, "remediation should name the failing subtask"
        assert "Findings" in note_text or "regex" in note_text.lower(), (
            "remediation should hint at the validator failure"
        )


        report_path.write_text(
            "# Report\n\n"
            "Some prose.\n\n"
            "## Findings\n\n"
            "- All requirements met.\n"
        )
        db.update_subtask(
            goal_id, "st_1", status="completed",
            result_summary="added Findings section",
        )
        return {
            "reply": "Added the Findings section and re-marked complete.",
            "rounds": 1, "tools_used": ["write_file", "subtask_update"],
            "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
        }

    monkeypatch.setattr(orchestrator, "run_orchestrator", _fake_run_orchestrator)


    async def _go():
        shutdown = asyncio.Event()
        await goal_runner.run(goal_id, shutdown)

    asyncio.run(_go())


    assert len(attempt_log) == 2, f"expected 2 attempts, got {len(attempt_log)}"

    assert attempt_log[0]["system_notes"] == []

    assert attempt_log[1]["system_notes"], "gate must inject remediation on retry"


    g = db.get_goal(goal_id)
    assert g["status"] == "done", (
        f"goal should be done after second attempt passes; got {g['status']!r}"
    )


    plan = db.get_goal_plan(goal_id)
    st = plan["subtasks"][0]
    assert st["status"] == "completed"
    assert st["validation_passed"] is True
    assert st["last_validation_failure"] is None


    assert "## Findings" in report_path.read_text()


    events = db.get_goal_events(goal_id)
    types = [e["event_type"] for e in events]
    assert "acceptance_gate_blocked" in types, (
        f"acceptance_gate_blocked event must be logged; got events: {types}"
    )


    assert types.count("subtask_completed") >= 1, (
        f"subtask_completed event(s) expected; got {types}"
    )


def test_e2e_exhausted_gate_marks_goal_failed(qwe_temp_data_dir, monkeypatch):
    """If the orchestrator never makes the validator pass, after
    MAX_GATE_ATTEMPTS the goal lands in ``failed`` with reason
    ``acceptance_gate_exhausted``. This is the safety stop —
    the orchestrator can't loop forever pretending to fix something."""
    workspace = Path(config.DATA_DIR) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    goal_id = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(goal_id, [{
        "title": "Create unreachable.txt",
        "description": "Must exist",
        "done_condition": {
            "kind": "files_exist",
            "spec": {"paths": [str(workspace / "unreachable.txt")]},
        },
    }])


    attempts = []

    def _fake_run_orchestrator(*, goal_id, ctx, system_notes=None, **kw):
        attempts.append(1)
        db.update_subtask(goal_id, "st_1", status="completed",
                          result_summary="claimed done")
        return {"reply": "done?", "rounds": 1, "tools_used": [],
                "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(orchestrator, "run_orchestrator", _fake_run_orchestrator)

    monkeypatch.setattr(goal_runner, "_gate_max_attempts", lambda: 3)

    async def _go():
        shutdown = asyncio.Event()
        await goal_runner.run(goal_id, shutdown)

    asyncio.run(_go())


    assert len(attempts) == 3, f"expected 3 attempts (cap), got {len(attempts)}"

    g = db.get_goal(goal_id)
    assert g["status"] == "failed", (
        f"goal must be failed after gate exhaustion; got {g['status']!r}"
    )
    assert "acceptance_gate_exhausted" in (g.get("error") or ""), (
        f"goal.error should mention acceptance_gate_exhausted; got {g.get('error')!r}"
    )


    assert not (workspace / "unreachable.txt").exists()
