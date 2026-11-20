"""Goal orchestrator — Phase 2 of the long-running agent runtime.

The orchestrator is the "main LLM" of a Goal. It maintains the plan
(via goal_plan_set / subtask_update), decides what to do next, and either
executes simple steps inline OR dispatches subagents (Phase 2c) for heavy
work. Subagents run with FRESH contexts so the orchestrator's window stays
small even across hours of work.

Architecture differences from chat-mode `agent.run`:

  - No `soul.py` prompt — we use `prompts/orchestrator.md` instead. The
    orchestrator is not a chat assistant.
  - No `auto_context` recall — facts live in `goal_facts`, the orchestrator
    fetches them explicitly via the `fact_get` tool.
  - Restricted tool set: orchestrator-management tools + lightweight ops
    (http_request, read/write_file, shell, memory_*). Browser tools and other
    heavy ops are reachable ONLY via `dispatch_subagent` (added in 2c).
  - No message persistence to the `messages` table — orchestrator state lives
    in `goal_checkpoints` and is restored from the latest checkpoint on resume.

The orchestrator's tools mutate goal state via the active TurnContext.goal_id
(read by tools._require_goal_id). That's why we must always pass `ctx` with
a non-None `goal_id` when calling run_orchestrator.
"""
from __future__ import annotations

import time
from pathlib import Path

import config
import db
import logger
import providers
import tools
from agent_budget import BudgetLimits
from agent_events import EventEmitter
from agent_loop import run_loop
from turn_context import TurnContext

_log = logger.get("orchestrator")


_PROMPT_PATH = Path(__file__).parent / "prompts" / "orchestrator.md"
_prompt_cache: str | None = None


def _load_system_prompt() -> str:
    global _prompt_cache
    if _prompt_cache is None:
        _prompt_cache = _PROMPT_PATH.read_text(encoding="utf-8")
    return _prompt_cache


_ORCHESTRATOR_TOOL_NAMES: set[str] = {

    "goal_plan_set", "subtask_update", "fact_save", "fact_get",
    "goal_attach_output",

    "memory_save", "memory_search",
    "http_request",
    "read_file", "write_file",
    "shell",
    "send_file",

    "dispatch_subagent",
}


_ORCHESTRATOR_EXCLUDED_TOOLS: set[str] = {
    "browser_open", "browser_close", "browser_snapshot",
    "browser_accessibility", "browser_click", "browser_fill",
    "browser_eval", "browser_wait_for", "browser_press_key",
    "browser_screenshot", "browser_back", "browser_forward",
    "browser_reload", "browser_set_visible", "browser_console",
    "browser_hover", "browser_select", "browser_drag",
    "browser_upload", "browser_tabs", "browser_tab_new",
    "browser_tab_switch", "browser_tab_close", "browser_network",
}


def _get_orchestrator_tools() -> list[dict]:
    """Return the OpenAI-format tool schemas the orchestrator is allowed to call.

    Three layers:
      1. The hard-coded core whitelist (_ORCHESTRATOR_TOOL_NAMES) — goal
         management + lightweight inline ops.
      2. **All user-installed skill tools.** If the user installed a
         ``linkedin_lead_gen`` skill, the orchestrator should be able to
         use its tools directly instead of dispatching a generic browser
         subagent that has to figure out LinkedIn from scratch. Skills
         are user-controlled (active via Settings UI / KV ``active_skills``)
         so they're trusted by definition.
      3. **All MCP tools.** Same trust model — user opted into the MCP
         server.

    Pulled fresh each turn so a skill activated mid-goal (or an MCP server
    started mid-goal) is immediately available.
    """
    all_tools = tools._get_all_tools_full()

    keep_names = set(_ORCHESTRATOR_TOOL_NAMES)


    _core_names = {t.get("function", {}).get("name") for t in tools.TOOLS}
    for t in all_tools:
        fn_name = t.get("function", {}).get("name")
        if fn_name and fn_name not in _core_names and fn_name not in _ORCHESTRATOR_EXCLUDED_TOOLS:
            keep_names.add(fn_name)

    seen: set[str] = set()
    result: list[dict] = []
    for t in all_tools:
        fn_name = t.get("function", {}).get("name", "")
        if fn_name in keep_names and fn_name not in seen:
            seen.add(fn_name)
            result.append(t)
    return result


def run_orchestrator(
    goal_id: str,
    ctx: TurnContext,
    system_notes: list[str] | None = None,
) -> dict:
    """Drive one goal from start (or last checkpoint) to terminal status.

    Synchronous — runs inside the worker's thread executor so the worker's
    asyncio loop stays responsive for heartbeats / signal handling.

    Returns a dict with at least::

        {"reply": str, "rounds": int, "tools_used": list[str], "cost_usd": float}

    ``ctx`` MUST have ``goal_id`` set so the orchestrator's tools can find
    the active goal.

    ``system_notes`` is an optional list of additional ``{role: system}``
    messages to inject for THIS invocation only — used by the acceptance
    gate in :mod:`goal_runner` to feed remediation back into the loop
    when a subtask's ``done_condition`` failed.

    Placement rule (so the model sees them at the right point in context):

      - **Fresh start** (no checkpoint): notes go AFTER the main system
        prompt but BEFORE the user input. They read as additional rules
        the orchestrator must obey from turn 1.
      - **Resume from checkpoint**: notes go at the END of ``messages``
        (most recent context). The orchestrator has been running for a
        while; the gate just told it what's still missing — that needs
        to be the freshest thing it sees on the next round.

    Notes are ephemeral — they're injected into the in-memory ``messages``
    list passed to ``run_loop``, never persisted. The next checkpoint
    saved by the round-complete callback captures whatever ``run_loop``
    leaves behind, including any new assistant/tool messages produced
    in response to the notes.
    """
    if not ctx.goal_id:
        raise ValueError("run_orchestrator requires ctx.goal_id")

    goal = db.get_goal(goal_id)
    if not goal:
        raise ValueError(f"goal {goal_id} not found")


    note_msgs: list[dict] = [
        {"role": "system", "content": note}
        for note in (system_notes or [])
        if isinstance(note, str) and note.strip()
    ]


    checkpoint = db.load_latest_checkpoint(goal_id)
    start_round = 0
    if checkpoint and checkpoint.get("messages"):
        messages = list(checkpoint["messages"])

        if note_msgs:
            messages.extend(note_msgs)
        start_round = checkpoint["round_num"]
        _log.info(f"[{goal_id}] resuming orchestrator from round {start_round}")
    else:
        system = _load_system_prompt()
        messages = [{"role": "system", "content": system}]


        ws_root = getattr(ctx, "workspace_root", None)
        if ws_root:
            messages.append({
                "role": "system",
                "content": (
                    f"WORKSPACE: This goal has a dedicated workspace at "
                    f"{ws_root}\n\n"
                    f"This dir starts EMPTY. Past goals' artifacts are NOT "
                    f"visible here — do NOT shell `ls ~/.castor/workspace/`, "
                    f"do NOT `cat` files from prior runs. Any relative path "
                    f"you write resolves under this dir. If you write "
                    f"`~/.castor/workspace/foo.csv` it lands here too. "
                    f"Build your plan from the user_input alone; the only "
                    f"files in scope are the ones you create this turn."
                ),
            })


        if note_msgs:
            messages.extend(note_msgs)
        messages.append({"role": "user", "content": goal["user_input"]})
        _log.info(f"[{goal_id}] starting orchestrator fresh")


    client = providers.get_client()
    model = providers.get_model()
    tool_schemas = _get_orchestrator_tools()
    _log.info(f"[{goal_id}] orchestrator tools: {[t['function']['name'] for t in tool_schemas]}")


    emitter = EventEmitter()
    emitter.on("status", lambda e: _log.info(f"[{goal_id}] {e.data.get('text', '')}"))


    if ctx.on_round_complete is None:
        ctx.on_round_complete = _default_checkpoint_callback(goal_id, start_round)


    budget_seconds = goal.get("budget_seconds")
    budget_usd = goal.get("budget_usd")
    started_at = goal.get("started_at") or time.time()
    have_budget = bool(budget_seconds) or bool(budget_usd)
    if have_budget and ctx.abort_event is not None:
        _user_cb = ctx.on_round_complete

        def _budget_aware_cb(round_num: int, msgs: list[dict]) -> None:

            if budget_seconds:
                elapsed = time.time() - float(started_at)
                if elapsed >= float(budget_seconds):
                    _log.warning(
                        f"[{goal_id}] wall-clock budget exceeded: "
                        f"{elapsed:.0f}s >= {budget_seconds}s — aborting"
                    )
                    db.log_goal_event(goal_id, "budget_exceeded", {
                        "kind": "wall_clock_seconds",
                        "elapsed_sec": int(elapsed),
                        "limit_sec": int(budget_seconds),
                    })
                    ctx.abort_event.set()


            if budget_usd:
                try:
                    spent = db.get_goal_total_cost(goal_id)
                except Exception:
                    spent = 0.0
                if spent >= float(budget_usd):
                    _log.warning(
                        f"[{goal_id}] USD budget exceeded: "
                        f"${spent:.4f} >= ${budget_usd:.4f} — aborting"
                    )
                    db.log_goal_event(goal_id, "budget_exceeded", {
                        "kind": "usd",
                        "spent_usd": spent,
                        "limit_usd": float(budget_usd),
                    })
                    ctx.abort_event.set()
            if _user_cb is not None:
                _user_cb(round_num, msgs)

        ctx.on_round_complete = _budget_aware_cb
        _log.info(
            f"[{goal_id}] budgets: "
            f"wall_clock={budget_seconds}s, usd=${budget_usd} "
            f"(started at {started_at:.0f})"
        )


    orch_max_turns = int(config.get("orchestrator_max_turns") or 80)
    budget = BudgetLimits(max_turns=orch_max_turns)
    turn_start = time.time()
    try:


        _orch_allowed = {t["function"]["name"] for t in tool_schemas if "function" in t}
        result = run_loop(
            client=client,
            model=model,
            messages=messages,
            tools=tool_schemas,
            emitter=emitter,
            budget=budget,
            temperature=0.3,
            presence_penalty=0.0,
            max_tokens=2048,
            tool_executor=tools.execute,
            ctx=ctx,
            allowed_tools=_orch_allowed,
        )
    except Exception:
        _log.exception(f"[{goal_id}] run_loop crashed")
        raise

    duration_ms = int((time.time() - turn_start) * 1000)
    reply = result.get("reply", "") or ""
    _log.info(
        f"[{goal_id}] orchestrator done: rounds={result.get('tool_calls', 'n/a')}, "
        f"reply_len={len(reply)}, duration_ms={duration_ms}"
    )

    return {
        "reply": reply,
        "rounds": result.get("rounds", 0),
        "tools_used": result.get("tool_calls", []),
        "cost_usd": result.get("cost_usd", 0.0),
        "prompt_tokens": result.get("prompt_tokens", 0),
        "completion_tokens": result.get("completion_tokens", 0),
    }


def _default_checkpoint_callback(goal_id: str, start_round: int):
    """Fallback checkpoint cadence when nobody else wired one up."""
    interval = max(1, int(config.get("checkpoint_round_interval") or 3))

    def _cb(round_num: int, messages: list[dict]) -> None:
        global_round = start_round + round_num
        if global_round <= 0 or (global_round % interval) != 0:
            return
        plan = db.get_goal_plan(goal_id) or {}
        facts = db.fact_get(goal_id)
        try:
            db.save_checkpoint(goal_id, global_round, subtask_index=-1,
                               messages=messages, plan=plan, facts=facts)
        except Exception:
            _log.exception(f"checkpoint failed for {goal_id} round {global_round}")
    return _cb
