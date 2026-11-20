"""Workstream B (acceptance gate) — done_condition + validator wiring.

Spec: docs/specs/2026-05-16-acceptance-gate.md §3.

Pinned contracts (each = one test below):

 1. set_goal_plan stores done_condition + adds validation_passed + last_validation_failure
 2. set_goal_plan rejects a malformed done_condition (validate_criterion gate)
 3. update_subtask(status=completed) with passing validator → status flips + validation_passed True
 4. update_subtask(status=completed) with failing validator → status STAYS + remediation written + attempts bumps
 5. update_subtask non-completed statuses bypass the validator
 6. update_subtask accepts validation_passed / last_validation_failure kwargs without changing status
 7. tool layer (_goal_plan_set_impl) requires done_condition on every subtask
 8. tool layer (_subtask_update_impl) returns the spec'd "NOT marked complete" remediation string
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import pytest


@contextmanager
def _patched_validator(monkeypatch, *, run_returns=(True, "")):
    """Pin ``goal_validators.run_validator`` so completion-gate tests are
    deterministic without depending on workstream-A's real implementation."""
    import goal_validators

    monkeypatch.setattr(goal_validators, "run_validator", lambda c, workspace_root=None: run_returns)
    yield


def _basic_subtask(title="Search for leads", *, kind="files_exist", spec=None):


    return {
        "title": title,
        "description": f"{title} (description body)",
        "done_condition": {
            "kind": kind,
            "spec": spec if spec is not None else {"paths": ["/tmp/leads.csv"]},
        },
    }


def test_set_goal_plan_stores_done_condition_and_new_fields(qwe_temp_data_dir):
    import db

    gid = db.create_goal(user_input="x", source="cli")
    plan = db.set_goal_plan(gid, [
        _basic_subtask("A"),
        _basic_subtask("B", kind="regex_in_file",
                       spec={"path": "leads.csv", "pattern": "row"}),
    ])
    assert len(plan["subtasks"]) == 2
    for st in plan["subtasks"]:

        assert isinstance(st["done_condition"], dict)
        assert "kind" in st["done_condition"] and "spec" in st["done_condition"]

        assert st["validation_passed"] is False
        assert st["last_validation_failure"] is None


    fetched = db.get_goal_plan(gid)
    assert fetched["subtasks"][0]["done_condition"]["kind"] == "files_exist"
    assert fetched["subtasks"][1]["done_condition"]["spec"]["pattern"] == "row"


def test_set_goal_plan_rejects_malformed_done_condition(qwe_temp_data_dir, monkeypatch):
    """A criterion that fails validate_criterion must raise — never land on disk."""
    import db
    import goal_validators


    def _validator(criterion):
        raise ValueError("missing 'spec' key (test stub forced)")

    monkeypatch.setattr(goal_validators, "validate_criterion", _validator)

    gid = db.create_goal(user_input="x", source="cli")
    with pytest.raises(ValueError, match="invalid done_condition"):
        db.set_goal_plan(gid, [_basic_subtask("A")])

    assert db.get_goal_plan(gid) is None


def test_update_subtask_completed_passes_validator(qwe_temp_data_dir, monkeypatch):
    import db

    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [_basic_subtask("A")])

    with _patched_validator(monkeypatch, run_returns=(True, "")):
        plan = db.update_subtask(gid, "st_1", status="completed",
                                 result_summary="found 47 rows")

    st = plan["subtasks"][0]
    assert st["status"] == "completed"
    assert st["validation_passed"] is True
    assert st["last_validation_failure"] is None
    assert st["finished_at"] is not None
    assert st["result_summary"] == "found 47 rows"


def test_update_subtask_completed_fails_validator_keeps_status(qwe_temp_data_dir,
                                                                monkeypatch):
    """The load-bearing acceptance-gate test.

    When the validator rejects a completion claim:
      - status MUST NOT flip to 'completed'
      - validation_passed False + last_validation_failure recorded
      - attempts is bumped (so retries don't spin forever silently)
      - finished_at MUST NOT be set
    """
    import db

    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [_basic_subtask("A")])

    db.update_subtask(gid, "st_1", status="in_progress")

    with _patched_validator(monkeypatch,
                            run_returns=(False, "File /tmp/leads.csv is empty")):
        plan = db.update_subtask(gid, "st_1", status="completed",
                                 result_summary="thought I was done")

    st = plan["subtasks"][0]

    assert st["status"] == "in_progress"
    assert st["finished_at"] is None

    assert st["validation_passed"] is False
    assert "File /tmp/leads.csv is empty" in st["last_validation_failure"]

    assert st["attempts"] == 1


    with _patched_validator(monkeypatch,
                            run_returns=(False, "still empty")):
        plan = db.update_subtask(gid, "st_1", status="completed")
    assert plan["subtasks"][0]["attempts"] == 2


    events = db.get_goal_events(gid)
    types = [e["event_type"] for e in events]
    assert "subtask_validation_failed" in types


def test_update_subtask_non_completed_bypasses_validator(qwe_temp_data_dir,
                                                         monkeypatch):
    """status=in_progress / failed / skipped must NOT invoke the validator —
    those statuses don't claim acceptance."""
    import db
    import goal_validators

    calls = []

    def _watcher(criterion, workspace_root=None):
        calls.append(criterion)
        return (False, "should never run")

    monkeypatch.setattr(goal_validators, "run_validator", _watcher)

    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [
        _basic_subtask("A"),
        _basic_subtask("B"),
        _basic_subtask("C"),
    ])

    db.update_subtask(gid, "st_1", status="in_progress")
    db.update_subtask(gid, "st_2", status="failed", result_summary="boom")
    db.update_subtask(gid, "st_3", status="skipped")

    # Validator was NEVER called.
    assert calls == []

    plan = db.get_goal_plan(gid)
    statuses = {st["id"]: st["status"] for st in plan["subtasks"]}
    assert statuses == {"st_1": "in_progress", "st_2": "failed", "st_3": "skipped"}


def test_update_subtask_validation_flag_kwargs_dont_change_status(qwe_temp_data_dir):
    """workstream-C goal_runner uses these kwargs to record probe results
    between rounds without flipping status — verify they're additive."""
    import db

    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [_basic_subtask("A")])
    db.update_subtask(gid, "st_1", status="in_progress")

    plan = db.update_subtask(
        gid, "st_1",
        validation_passed=False,
        last_validation_failure="probe says file is 0 bytes",
    )
    st = plan["subtasks"][0]
    assert st["status"] == "in_progress"
    assert st["validation_passed"] is False
    assert st["last_validation_failure"] == "probe says file is 0 bytes"


    plan = db.update_subtask(gid, "st_1", validation_passed=True,
                             last_validation_failure="")
    st = plan["subtasks"][0]
    assert st["status"] == "in_progress"
    assert st["validation_passed"] is True

    assert st["last_validation_failure"] is None


def _bind_active_goal(monkeypatch, goal_id):
    """Patch tools._require_goal_id so the impls think we're in a goal turn."""
    import tools as t

    monkeypatch.setattr(t, "_require_goal_id", lambda: goal_id)


def test_tool_goal_plan_set_requires_done_condition(qwe_temp_data_dir, monkeypatch):
    """LLM-facing path: every subtask in args MUST carry a done_condition."""
    import db
    import tools as t

    gid = db.create_goal(user_input="x", source="cli")
    _bind_active_goal(monkeypatch, gid)


    out = t._goal_plan_set_impl({"subtasks": [
        {"title": "ok one", "description": "...",
         "done_condition": {"kind": "files_exist", "spec": {"paths": ["/x"]}}},
        {"title": "bad one", "description": "..."},
    ]})
    assert out.startswith("Error:")
    assert "done_condition" in out
    assert "'bad one'" in out
    # Plan never landed.
    assert db.get_goal_plan(gid) is None


    out2 = t._goal_plan_set_impl({"subtasks": [
        {"title": "ok one", "description": "...",
         "done_condition": {"kind": "files_exist", "spec": {"paths": ["/x"]}}},
        {"title": "ok two", "description": "...",
         "done_condition": {"kind": "shell_returns_zero", "spec": {"cmd": "true"}}},
    ]})
    assert out2.startswith("Plan set with 2 subtask(s)")
    plan = db.get_goal_plan(gid)
    assert plan is not None
    assert plan["subtasks"][1]["done_condition"]["kind"] == "shell_returns_zero"


def test_tool_subtask_update_surfaces_remediation_on_validator_failure(
        qwe_temp_data_dir, monkeypatch):
    """Spec §3 exact wording: 'Subtask {id} NOT marked complete: validator failed.
    Remediation: ...'."""
    import db
    import goal_validators
    import tools as t

    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [_basic_subtask("A")])
    _bind_active_goal(monkeypatch, gid)

    monkeypatch.setattr(
        goal_validators, "run_validator",
        lambda c, workspace_root=None: (False, "expected leads.csv to exist; got nothing"),
    )

    out = t._subtask_update_impl({
        "subtask_id": "st_1",
        "status": "completed",
        "result_summary": "I think I did it",
    })
    assert "Subtask st_1 NOT marked complete: validator failed." in out
    assert "Remediation:" in out
    assert "expected leads.csv to exist; got nothing" in out


    plan = db.get_goal_plan(gid)
    assert plan["subtasks"][0]["status"] == "pending"
    assert plan["subtasks"][0]["validation_passed"] is False
