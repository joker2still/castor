"""Per-fire routine run history + missed-run detection.

v0.17.32 added a routine_runs table so users can see whether a routine
actually fired vs was skipped because the server was offline at the
scheduled time. Before this, ``last_run`` only reflected the most
recent attempt — a weekly routine that failed to fire three weeks
running looked identical to one that had fired yesterday.

v0.19.0 migrated from routine_runs to agent_runs: ok/err rows are now
written by agent_loop.run_loop when ctx.cron_id is set; scheduler only
writes missed/skipped rows via db.insert_skipped_run.  The test mocks
simulate what agent_loop does by writing the row through db directly.
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def sched(qwe_temp_data_dir):
    import importlib
    import sys
    for m in ("config", "db", "scheduler"):
        if m in sys.modules:
            importlib.reload(sys.modules[m])
        else:
            importlib.import_module(m)
    s = sys.modules["scheduler"]
    s._callbacks.clear()
    s._ROUTINE_FIRE_LOCKS.clear()
    return s


def test_successful_fire_logs_ok_row(sched, monkeypatch):
    """A successful agent.run records an agent_runs row with status=ok.

    The real path: agent_loop.run_loop writes the row when ctx.cron_id is set.
    The mock simulates that by writing the row through db directly.
    """
    import agent
    import db

    class _FakeResult:
        def __init__(self, r): self.reply = r

    def _fake_run(u, thread_id=None, source="cli", ctx=None, **kw):

        cron_id_val = ctx.cron_id if ctx is not None else None
        rid = db.insert_agent_run(
            thread_id=thread_id or "",
            cron_id=cron_id_val,
            source="routine",
            scheduled_at=time.time(),
            started_at=time.time(),
            status="running",
        )
        db.finalize_agent_run(
            rid,
            finished_at=time.time(),
            duration_ms=10,
            status="ok",
            error=None,
            result_preview="all good",
            input_tokens=0,
            output_tokens=0,
            cost_usd=None,
        )
        return _FakeResult("all good")

    monkeypatch.setattr(agent, "run", _fake_run)

    r = sched.add("alpha", "ping", "every 1h", skip_dry_run=True)
    cron_id = [t["id"] for t in sched.list_tasks() if t["name"] == "alpha"][0]
    thread_id = r["thread_id"]
    sched._execute_routine("ping", "alpha", cron_id, thread_id)

    runs = sched.list_runs(cron_id)
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert runs[0]["duration_ms"] >= 0


def test_crashed_fire_logs_err_row(sched, monkeypatch):
    """A crashing agent.run still results in an agent_runs row with status=err.

    When agent.run raises, the exception propagates back to _execute_routine;
    the run is considered 'err'. Since we're mocking agent.run (bypassing
    agent_loop), the mock itself writes the err row to agent_runs to simulate
    what agent_loop would do in the crash path.
    """
    import agent
    import db

    def _fake_crash(u, thread_id=None, source="cli", ctx=None, **kw):

        cron_id_val = ctx.cron_id if ctx is not None else None
        rid = db.insert_agent_run(
            thread_id=thread_id or "",
            cron_id=cron_id_val,
            source="routine",
            scheduled_at=time.time(),
            started_at=time.time(),
            status="running",
        )
        db.finalize_agent_run(
            rid,
            finished_at=time.time(),
            duration_ms=5,
            status="err",
            error="RuntimeError: nope",
            result_preview="",
            input_tokens=0,
            output_tokens=0,
            cost_usd=None,
        )
        raise RuntimeError("nope")

    monkeypatch.setattr(agent, "run", _fake_crash)

    r = sched.add("crashy", "ping", "every 1h", skip_dry_run=True)
    cron_id = [t["id"] for t in sched.list_tasks() if t["name"] == "crashy"][0]
    sched._execute_routine("ping", "crashy", cron_id, r["thread_id"])

    runs = sched.list_runs(cron_id)
    assert len(runs) == 1
    assert runs[0]["status"] == "err"
    assert "nope" in runs[0]["error"]


def test_concurrent_fire_logs_skipped(sched, monkeypatch):
    """When the per-thread lock is held, the second call logs status=skipped.

    The first fire: mock writes an ok row to agent_runs (simulating agent_loop).
    The second fire: scheduler writes a skipped row to agent_runs directly.
    """
    import agent
    import db
    import threading as _th


    hold = _th.Event()

    class _FakeResult:
        def __init__(self, r): self.reply = r

    def _slow(u, thread_id=None, source="cli", ctx=None, **kw):
        cron_id_val = ctx.cron_id if ctx is not None else None
        rid = db.insert_agent_run(
            thread_id=thread_id or "",
            cron_id=cron_id_val,
            source="routine",
            scheduled_at=time.time(),
            started_at=time.time(),
            status="running",
        )
        hold.wait(timeout=5)
        db.finalize_agent_run(
            rid,
            finished_at=time.time(),
            duration_ms=100,
            status="ok",
            error=None,
            result_preview="ok",
            input_tokens=0,
            output_tokens=0,
            cost_usd=None,
        )
        return _FakeResult("ok")

    monkeypatch.setattr(agent, "run", _slow)

    r = sched.add("busy", "ping", "every 1h", skip_dry_run=True)
    cron_id = [t["id"] for t in sched.list_tasks() if t["name"] == "busy"][0]
    tid = r["thread_id"]


    th = _th.Thread(target=lambda: sched._execute_routine("ping", "busy", cron_id, tid))
    th.start()

    time.sleep(0.1)


    out = sched._execute_routine("ping", "busy", cron_id, tid)
    assert out == ""

    hold.set()
    th.join(timeout=10)

    runs = sched.list_runs(cron_id)
    statuses = [r["status"] for r in runs]
    assert "skipped" in statuses
    assert "ok" in statuses


def test_detect_missed_runs_first_boot_is_noop(sched):
    """On the very first boot (no stamp yet), don't invent historical
    misses — just stamp and move on."""
    import db
    db.kv_set("scheduler:last_check", "")
    n = sched.detect_missed_runs()
    assert n == 0


def test_detect_missed_logs_rows_for_offline_gap(sched, monkeypatch):
    """When next_run is well before last_check's future → server was
    offline across scheduled slots → log each as missed."""
    import db
    import agent

    class _FakeResult:
        def __init__(self, r): self.reply = r
    monkeypatch.setattr(agent, "run", lambda u, **kw: _FakeResult("ok"))

    sched.add("hourly", "ping", "every 1h", skip_dry_run=True)
    cron_id = [t["id"] for t in sched.list_tasks() if t["name"] == "hourly"][0]

    now = time.time()


    db.kv_set("scheduler:last_check", str(now - 5.5 * 3600))
    db.execute("UPDATE scheduled_tasks SET next_run=? WHERE id=?",
               (now + 1800, cron_id))

    n = sched.detect_missed_runs()

    assert n == 5
    runs = sched.list_runs(cron_id, limit=20)
    assert all(r["status"] == "missed" for r in runs)
    assert len(runs) == 5


def test_detect_missed_caps_at_ten(sched, monkeypatch):
    """A very long outage doesn't spam hundreds of rows — capped at 10
    per routine so the user doesn't have to scroll forever."""
    import db
    sched.add("frequent", "ping", "every 10m", skip_dry_run=True)
    cron_id = [t["id"] for t in sched.list_tasks() if t["name"] == "frequent"][0]

    now = time.time()

    db.kv_set("scheduler:last_check", str(now - 24 * 3600))
    db.execute("UPDATE scheduled_tasks SET next_run=? WHERE id=?",
               (now + 60, cron_id))

    sched.detect_missed_runs()
    runs = sched.list_runs(cron_id, limit=50)
    assert len(runs) == 10
    assert all(r["status"] == "missed" for r in runs)


def test_detect_missed_skips_non_routines(sched, monkeypatch):
    """Heartbeat / synthesis are infrastructure; detect_missed should
    leave them alone — they self-correct on the next tick."""
    import db

    hb_next = time.time() - 3600
    db.execute(
        "INSERT INTO scheduled_tasks (name, task, schedule, next_run, repeat, enabled) "
        "VALUES (?, ?, ?, ?, 1, 1)",
        (sched.HEARTBEAT_TASK_NAME, sched.HEARTBEAT_TASK_NAME, "every 30m", hb_next),
    )
    db.kv_set("scheduler:last_check", str(time.time() - 3 * 3600))

    sched.detect_missed_runs()
    hb_id = db.fetchone("SELECT id FROM scheduled_tasks WHERE name=?",
                        (sched.HEARTBEAT_TASK_NAME,))[0]
    assert sched.list_runs(hb_id) == []


def test_routine_fire_does_not_persist_fake_user_message(sched, monkeypatch):
    """Each fire must NOT inflate the thread with a fake 'user typed the
    task' row. Only the assistant reply persists.

    Background: users were complaining that scheduled fires made the
    routine thread look like they had typed the same task 50 times.
    Phase: agent.run(save_user_msg=False) skips the user-row save; the
    LLM still sees the task in its messages array via _build_messages.
    """
    import agent
    import db


    class _FakeResult:
        def __init__(self, r): self.reply = r

    captured = {"save_user_msg": None}

    def _fake_run(user_input, thread_id=None, source="cli",
                   save_user_msg=True, **kw):
        captured["save_user_msg"] = save_user_msg

        if save_user_msg:
            db.save_message("user", user_input, thread_id=thread_id)
        db.save_message("assistant", "ok done", thread_id=thread_id)
        return _FakeResult("ok done")

    monkeypatch.setattr(agent, "run", _fake_run)

    r = sched.add("clean", "summarise yesterday", "every 1h", skip_dry_run=True)
    cron_id = [t["id"] for t in sched.list_tasks() if t["name"] == "clean"][0]
    tid = r["thread_id"]

    sched._execute_routine("summarise yesterday", "clean", cron_id, tid)


    assert captured["save_user_msg"] is False, (
        "_execute_routine must call agent.run(save_user_msg=False)"
    )


    rows = db.fetchall(
        "SELECT role FROM messages WHERE thread_id=? ORDER BY id",
        (tid,),
    )
    roles = [r[0] for r in rows]
    assert "user" not in roles, (
        f"routine fire persisted a user message: {roles}"
    )
    assert "assistant" in roles


def test_routine_fire_lets_user_clarifications_persist(sched, monkeypatch):
    """Real user clarifications typed into a routine thread must still
    persist normally. Only routine-injected user turns are skipped."""
    import agent
    import db

    class _FakeResult:
        def __init__(self, r): self.reply = r

    def _fake_run(user_input, thread_id=None, source="cli",
                   save_user_msg=True, **kw):

        if save_user_msg:
            db.save_message("user", user_input, thread_id=thread_id)
        db.save_message("assistant", "noted", thread_id=thread_id)
        return _FakeResult("noted")

    monkeypatch.setattr(agent, "run", _fake_run)

    r = sched.add("with-clarif", "do thing", "every 1h", skip_dry_run=True)
    tid = r["thread_id"]


    sched._execute_routine("do thing", "with-clarif",
                            [t["id"] for t in sched.list_tasks() if t["name"] == "with-clarif"][0],
                            tid)


    agent.run("actually skip weekends", thread_id=tid, source="web")

    rows = db.fetchall(
        "SELECT role, content FROM messages WHERE thread_id=? ORDER BY id",
        (tid,),
    )

    user_contents = [c for role, c in rows if role == "user"]
    assert "actually skip weekends" in user_contents
    assert "do thing" not in user_contents


def test_list_tasks_includes_recent_counts(sched, monkeypatch):
    import agent
    import db

    class _FakeResult:
        def __init__(self, r): self.reply = r

    def _fake_run(u, thread_id=None, source="cli", ctx=None, **kw):

        cron_id_val = ctx.cron_id if ctx is not None else None
        rid = db.insert_agent_run(
            thread_id=thread_id or "",
            cron_id=cron_id_val,
            source="routine",
            scheduled_at=time.time(),
            started_at=time.time(),
            status="running",
        )
        db.finalize_agent_run(
            rid,
            finished_at=time.time(),
            duration_ms=10,
            status="ok",
            error=None,
            result_preview="done",
            input_tokens=0,
            output_tokens=0,
            cost_usd=None,
        )
        return _FakeResult("done")

    monkeypatch.setattr(agent, "run", _fake_run)

    r = sched.add("counted", "ping", "every 1h", skip_dry_run=True)
    cron_id = [t["id"] for t in sched.list_tasks() if t["name"] == "counted"][0]
    sched._execute_routine("ping", "counted", cron_id, r["thread_id"])
    sched._execute_routine("ping", "counted", cron_id, r["thread_id"])

    entry = next(t for t in sched.list_tasks() if t["id"] == cron_id)
    assert "recent" in entry
    assert entry["recent"]["counts"]["ok"] == 2
    assert entry["recent"]["series"] == ["ok", "ok"]
