"""Per-turn request context — carries state that used to live in agent module globals.

castor runs concurrent turns from multiple sources (web WebSocket, Telegram
bot, CLI). Before TurnContext, per-request state was stashed in module-level
globals on ``agent.py`` / ``tools.py`` — which meant turn A's image path /
callbacks could stomp turn B's when both fired in the same process.

Every per-request knob now lives on the :class:`TurnContext` dataclass, which
is threaded through ``agent.run`` → ``agent._run_inner`` → ``agent_loop.run_loop``.
Helpers that need the active context (e.g. the streaming emit functions inside
``agent.py``) read it off a :class:`contextvars.ContextVar` so we don't have to
pass ``ctx`` through every internal helper.

What does **not** live here:

- ``_structured_output_failed`` — that tracks provider capabilities (a 400 from
  one provider is a fact about the provider, not about a turn).
- ``_compaction_lock`` — shared across turns by design.
"""
from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional


ContentCB = Callable[[str], None]
ThinkingCB = Callable[[str], None]
StatusCB = Callable[[str], None]

ToolCallCB = Callable[[str, str, str], None]

RecallCB = Callable[[list[dict]], None]


RoundCompleteCB = Callable[[int, list[dict]], None]


@dataclass
class TurnContext:
    """State that belongs to a single agent turn.

    Callers that care about concurrency (web server, telegram bot) build one
    of these per request and pass it into :func:`agent.run`. Callers that
    don't (CLI, tests) can rely on :func:`agent.run` constructing a default
    one for them.
    """


    abort_event: threading.Event = field(default_factory=threading.Event)


    on_content: Optional[ContentCB] = None
    on_thinking: Optional[ThinkingCB] = None
    on_status: Optional[StatusCB] = None
    on_tool_call: Optional[ToolCallCB] = None
    on_recall: Optional[RecallCB] = None


    image_path: Optional[str] = None

    file_meta: Optional[dict] = None


    source: str = "cli"

    session_id: Optional[str] = None


    cron_id: Optional[int] = None


    resumed_from_run_id: Optional[int] = None


    goal_id: Optional[str] = None
    on_round_complete: Optional[RoundCompleteCB] = None


    workspace_root: Optional[str] = None


    def emit_content(self, text: str) -> None:
        cb = self.on_content
        if cb is None:
            return
        try:
            cb(text)
        except Exception:
            pass

    def emit_thinking(self, text: str) -> None:
        cb = self.on_thinking
        if cb is None:
            return
        try:
            cb(text)
        except Exception:
            pass

    def emit_status(self, text: str) -> None:
        cb = self.on_status
        if cb is None:
            return
        try:
            cb(text)
        except Exception:
            pass

    def emit_tool_call(self, name: str, args_preview: str, result_preview: str = "") -> None:
        cb = self.on_tool_call
        if cb is None:
            return
        try:
            cb(name, args_preview, result_preview)
        except Exception:
            pass

    def emit_recall(self, memories: list[dict]) -> None:
        cb = self.on_recall
        if cb is None or not memories:
            return
        try:
            cb(memories)
        except Exception:
            pass

    def emit_round_complete(self, round_num: int, messages: list[dict]) -> None:
        cb = self.on_round_complete
        if cb is None:
            return
        try:
            cb(round_num, messages)
        except Exception:
            # Checkpointing is best-effort — never break the agent loop.
            pass


_NULL_CTX = TurnContext()

_current_turn_ctx: contextvars.ContextVar[Optional[TurnContext]] = contextvars.ContextVar(
    "castor_turn_ctx", default=None
)


def get_current() -> TurnContext:
    """Return the active :class:`TurnContext` for this task, or a no-op ctx."""
    ctx = _current_turn_ctx.get()
    return ctx if ctx is not None else _NULL_CTX


def set_current(ctx: Optional[TurnContext]) -> contextvars.Token:
    """Install *ctx* as the active context. Returns a token for :func:`reset`."""
    return _current_turn_ctx.set(ctx)


def reset(token: contextvars.Token) -> None:
    """Pop back to whatever context was active before :func:`set_current`."""
    _current_turn_ctx.reset(token)


__all__ = [
    "TurnContext",
    "get_current",
    "set_current",
    "reset",
]
