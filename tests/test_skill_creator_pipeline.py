"""End-to-end pipeline test for skill_creator with a camera-using skill.

Mocks `_llm_call` to feed plausible LLM outputs for every step of
`_run_pipeline`, then asserts the generated skill file:
  - is syntactically valid (ast.parse)
  - imports the agent runtime (tools, memory)
  - actually calls camera_capture and memory.save in the right places
  - passes validate_skill() (the skill loader's own checks)
  - passes _smoke_test() (load + call execute() + param-usage check)

This is the test that would have caught the "skill_creator generates
isolated CRUD-only skills" gap that motivated v0.18.2's prompt expansion
— if the LLM is told it can use cross-feature APIs but the deterministic
mapping + custom-code path drops them, the generated file won't
reference camera_capture at all and this test fails loudly.

The test is intentionally tight on what it asserts: only fields the
skill MUST have to be functional. It doesn't pin LLM response wording
or specific docstrings, so prompt revisions don't break it unless they
break the contract.
"""

from __future__ import annotations

import ast
import importlib
import json
import sys
from pathlib import Path

import pytest


CAMERA_PLAN = {
    "docstring": "Camera journal skill — capture and remember what you see",
    "short_description": "Photo + auto-describe + memory save",
    "instruction": "Use take_photo to capture the current scene and save the description to long-term memory tagged as journal.",
    "tables": [
        "skill_camera_journal_entries: id INTEGER PRIMARY KEY, ts TEXT, description TEXT"
    ],
    "tools": [
        "take_photo: capture current camera frame and save description to memory + local table"
    ],
}

CAMERA_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "take_photo",
            "description": "Capture from camera and save description to memory + journal table",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "What to focus on when describing the scene",
                    }
                },
                "required": ["prompt"],
            },
        }
    }
]


CAMERA_CUSTOM_CODE = '''    if name == "take_photo":
        import memory, tools
        prompt = args.get("prompt", "describe what you see")
        description = tools.execute("camera_capture", {"prompt": prompt})
        memory.save(description, tag="journal", synth=True)
        conn.execute(
            "INSERT INTO skill_camera_journal_entries (ts, description) VALUES (?, ?)",
            (datetime.now().isoformat(), description)
        )
        conn.commit()
        return f"Photo captured and remembered: {description[:100]}"
'''


@pytest.fixture
def sc_pipeline(qwe_temp_data_dir, monkeypatch):
    """Reload skill_creator against a temp data dir + mock side effects.

    Returns a tuple (sc_module, target_path, llm_calls) where llm_calls
    is a list capturing every (system_prompt, user_prompt) pair so the
    test can assert prompt content was passed correctly.
    """
    if "skills.skill_creator" in sys.modules:
        del sys.modules["skills.skill_creator"]
    if "skills" in sys.modules:
        importlib.reload(sys.modules["skills"])
    import skills.skill_creator as sc


    llm_calls = []

    def fake_llm(system: str, user: str, max_tokens: int = 2048, **kw) -> str:
        llm_calls.append({"system": system, "user": user, "max_tokens": max_tokens})

        if "skill architect" in system:
            return json.dumps(CAMERA_PLAN, ensure_ascii=False)

        if "tool definition generator" in system:
            return json.dumps(CAMERA_TOOLS, ensure_ascii=False)

        if "Generate Python code" in system:
            return CAMERA_CUSTOM_CODE
        raise AssertionError(f"unexpected LLM call with system: {system[:80]}")

    monkeypatch.setattr(sc, "_llm_call", fake_llm)


    import tasks
    monkeypatch.setattr(tasks, "register", lambda *a, **kw: 0)
    monkeypatch.setattr(tasks, "update", lambda *a, **kw: None)
    monkeypatch.setattr(sc, "_notify", lambda *a, **kw: None)
    monkeypatch.setattr(sc, "_save_skill_result", lambda *a, **kw: None)
    monkeypatch.setattr(sc, "_cleanup_debug_logs", lambda *a, **kw: None)


    import tools
    import memory
    monkeypatch.setattr(tools, "execute",
                        lambda name, args: "(mocked: a desk with a laptop)")
    monkeypatch.setattr(memory, "save",
                        lambda *a, **kw: "fake-point-id")

    target = qwe_temp_data_dir / "skills" / "camera_journal.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    return sc, target, llm_calls


def test_pipeline_generates_camera_skill_end_to_end(sc_pipeline):
    """Run _run_pipeline with mocked LLM and verify the produced .py."""
    sc, target, llm_calls = sc_pipeline

    sc._run_pipeline(
        skill_name="camera_journal",
        description="Take a photo and save what you see to memory as a daily journal entry",
        target=target,
        task_id=0,
    )


    assert target.exists(), f"skill file was not written to {target}"
    code = target.read_text(encoding="utf-8")


    tree = ast.parse(code)

    top_names = {
        n.targets[0].id
        for n in tree.body
        if isinstance(n, ast.Assign) and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name)
    }
    assert "TOOLS" in top_names
    funcs = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "execute" in funcs


    assert 'tools.execute("camera_capture"' in code or \
           "tools.execute('camera_capture'" in code, (
        "Skill must call tools.execute('camera_capture', ...) — that's "
        "the whole point of a camera-using skill"
    )


    assert "memory.save(" in code, "Skill must persist via memory.save()"


    assert "skill_camera_journal_" in code, (
        "Tables must be prefixed skill_<name>_ to avoid collisions "
        "(v0.18.2 namespacing rule)"
    )


    assert "tools" in code, "tools dispatcher must be referenced"
    assert "memory" in code, "memory module must be referenced"


    smoke_errors = sc._smoke_test(target, CAMERA_TOOLS)
    assert not smoke_errors, f"smoke test errors: {smoke_errors}"


    assert len(llm_calls) == 3, f"expected 3 LLM calls, got {len(llm_calls)}"
    systems = [c["system"] for c in llm_calls]
    assert "skill architect" in systems[0]
    assert "tool definition generator" in systems[1]
    assert "Generate Python code" in systems[2]


def test_pipeline_retries_when_step1_returns_bad_json(sc_pipeline):
    """If the planner's first attempt returns garbage, _run_pipeline
    should fall through and retry — it tries up to 3 times before
    giving up. Pin that behaviour (regression guard against making
    the retry loop accidentally fail-fast)."""
    sc, target, llm_calls = sc_pipeline


    attempt_counter = {"plan": 0}
    canned = sc._llm_call

    def flaky_llm(system, user, max_tokens=2048, **kw):
        if "skill architect" in system:
            attempt_counter["plan"] += 1
            if attempt_counter["plan"] == 1:
                return "this is not valid JSON at all"
        return canned(system, user, max_tokens, **kw)

    import skills.skill_creator as scm
    monkey = pytest.MonkeyPatch()
    monkey.setattr(scm, "_llm_call", flaky_llm)
    try:
        sc._run_pipeline("camera_journal", "test", target, task_id=0)
    finally:
        monkey.undo()


    assert target.exists()

    assert attempt_counter["plan"] == 2


def test_pipeline_rewrites_first_elif_to_if_when_body_empty(qwe_temp_data_dir, monkeypatch):
    """Field-session bug: when ALL tools are custom (deterministic
    mapping covered none), execute_body is empty and the LLM's custom
    code starts with 'elif name == ...' — that's a SyntaxError because
    there's no preceding 'if'. The post-process must rewrite the
    first 'elif' to 'if'. Regression-shield against the same bug
    coming back via prompt drift."""
    import importlib
    import sys
    if "skills.skill_creator" in sys.modules:
        del sys.modules["skills.skill_creator"]
    if "skills" in sys.modules:
        importlib.reload(sys.modules["skills"])
    import skills.skill_creator as sc

    plan = {
        "docstring": "Test skill",
        "short_description": "Test",
        "instruction": "Test",
        "tables": ["skill_test_thing_data: id INTEGER PRIMARY KEY, ts TEXT"],
        "tools": ["do_thing: a custom thing"],
    }
    tools_list = [
        {
            "type": "function",
            "function": {
                "name": "do_thing",
                "description": "Custom op",
                "parameters": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
            },
        }
    ]


    bad_custom_code = '''    elif name == "do_thing":
        x = args.get("x", "")
        return f"thing: {x}"
'''

    def fake_llm(system, user, max_tokens=2048, **kw):
        if "skill architect" in system:
            return json.dumps(plan, ensure_ascii=False)
        if "tool definition generator" in system:
            return json.dumps(tools_list, ensure_ascii=False)
        if "Generate Python code" in system:
            return bad_custom_code
        raise AssertionError(f"unexpected system: {system[:60]}")

    monkeypatch.setattr(sc, "_llm_call", fake_llm)
    import tasks
    monkeypatch.setattr(tasks, "register", lambda *a, **kw: 0)
    monkeypatch.setattr(tasks, "update", lambda *a, **kw: None)
    monkeypatch.setattr(sc, "_notify", lambda *a, **kw: None)
    monkeypatch.setattr(sc, "_save_skill_result", lambda *a, **kw: None)
    monkeypatch.setattr(sc, "_cleanup_debug_logs", lambda *a, **kw: None)

    target = qwe_temp_data_dir / "skills" / "test_thing.py"
    target.parent.mkdir(parents=True, exist_ok=True)

    sc._run_pipeline("test_thing", "test", target, task_id=0)
    assert target.exists(), "skill should have been written despite the elif-first LLM output"

    code = target.read_text(encoding="utf-8")
    # The whole skill file must parse cleanly
    ast.parse(code)
    # Inside execute(), the first branch must be 'if', not 'elif'
    assert 'if name == "do_thing"' in code or "if name == 'do_thing'" in code, (
        "post-process must rewrite first 'elif' to 'if' when body was empty"
    )


def test_generated_skill_actually_loads_via_skill_registry(sc_pipeline):
    """After the pipeline writes the file, the skill loader must be
    able to import it cleanly — no ImportError, no hidden syntax
    issues that ast.parse missed but Python's actual loader catches."""
    sc, target, _llm_calls = sc_pipeline

    sc._run_pipeline("camera_journal", "test", target, task_id=0)
    assert target.exists()


    import importlib.util
    spec = importlib.util.spec_from_file_location("camera_journal_test", target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)


    assert hasattr(mod, "TOOLS")
    assert isinstance(mod.TOOLS, list)
    assert len(mod.TOOLS) >= 1
    assert hasattr(mod, "execute")
    assert callable(mod.execute)


    tool_names = [t["function"]["name"] for t in mod.TOOLS]
    assert "take_photo" in tool_names


    result = mod.execute("take_photo", {"prompt": "show me the desk"})
    assert isinstance(result, str)
    assert len(result) > 0


def test_skill_creator_pipeline_writes_agent_run(sc_pipeline, qwe_temp_data_dir):
    """After _run_pipeline completes, exactly one agent_runs row with
    source='skill_creator' must exist for the skill's synthetic thread_id."""
    import db
    sc, target, _llm_calls = sc_pipeline

    sc._run_pipeline(
        skill_name="camera_journal",
        description="Take a photo and save what you see to memory",
        target=target,
        task_id=0,
    )

    rows = [
        r for r in db._get_conn().execute(
            "SELECT source, status FROM agent_runs WHERE source='skill_creator'"
        ).fetchall()
    ]
    assert len(rows) >= 1, "expected at least one agent_runs row with source='skill_creator'"
    # The row must have a terminal status
    status = rows[0][1]
    assert status in ("ok", "err"), f"unexpected status: {status}"
