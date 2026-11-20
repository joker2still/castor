"""Tests for `agent_loop._extract_tool_from_text`.

This is the layer-2 fallback that pulls a tool call out of the model's
*text* output when the provider didn't emit a native `delta.tool_calls`.
Five patterns are recognised — see the function's body for the exact
shapes. These tests pin each one and the negative cases.

Pattern 5 was added to fix #10 — some LM Studio / Ollama-served Qwen
variants emit `!<function_call:{"call": ..., "arguments": ...}>` and
without that pattern the call would render as raw text and the model
would loop forever ("infinite reply" symptom).
"""

from __future__ import annotations

from agent_loop import _extract_tool_from_text

TOOLS = {"browser_open", "memory_search", "shell", "write_file"}


def test_pattern1_qwen_tool_call_block():
    text = '<tool_call>{"name": "shell", "arguments": {"command": "ls"}}</tool_call>'
    assert _extract_tool_from_text(text, TOOLS) == ("shell", {"command": "ls"})


def test_pattern1_unknown_tool_returns_none():
    text = '<tool_call>{"name": "not_a_real_tool", "arguments": {}}</tool_call>'
    assert _extract_tool_from_text(text, TOOLS) is None


def test_pattern2_function_call_with_dict():
    text = 'I will run shell({"command": "echo hi"}) for you.'
    assert _extract_tool_from_text(text, TOOLS) == ("shell", {"command": "echo hi"})


def test_pattern3_function_call_with_kwargs():
    text = 'Calling browser_open(url="https://example.com")'
    assert _extract_tool_from_text(text, TOOLS) == (
        "browser_open", {"url": "https://example.com"})


def test_pattern4_browser_with_nearby_url():
    text = "Let me use browser_open and visit https://example.com"
    assert _extract_tool_from_text(text, TOOLS) == (
        "browser_open", {"url": "https://example.com"})


def test_pattern4_non_browser_tool_with_url_returns_none():

    text = "Run shell on https://example.com"
    assert _extract_tool_from_text(text, TOOLS) is None


def test_pattern5_function_call_wrapper_with_call_key():

    text = '!<function_call:{"call": "memory_search", "arguments": {"query": "user goals"}}>'
    assert _extract_tool_from_text(text, TOOLS) == (
        "memory_search", {"query": "user goals"})


def test_pattern5_accepts_name_key_too():

    text = '!<function_call:{"name": "shell", "arguments": {"command": "pwd"}}>'
    assert _extract_tool_from_text(text, TOOLS) == ("shell", {"command": "pwd"})


def test_pattern5_arguments_null_becomes_empty_dict():

    text = '!<function_call:{"call": "memory_search", "arguments": null}>'
    assert _extract_tool_from_text(text, TOOLS) == ("memory_search", {})


def test_pattern5_arguments_string_becomes_empty_dict():


    text = '!<function_call:{"call": "shell", "arguments": "ls -la"}>'
    assert _extract_tool_from_text(text, TOOLS) == ("shell", {})


def test_pattern5_unknown_tool_returns_none():
    text = '!<function_call:{"call": "magical_tool", "arguments": {}}>'
    assert _extract_tool_from_text(text, TOOLS) is None


def test_pattern5_missing_call_and_name_returns_none():
    text = '!<function_call:{"arguments": {"x": 1}}>'
    assert _extract_tool_from_text(text, TOOLS) is None


def test_pattern5_invalid_json_returns_none():
    text = '!<function_call:{not valid json}>'
    assert _extract_tool_from_text(text, TOOLS) is None


def test_pattern5_nested_dict_in_arguments():


    text = ('!<function_call:{"call": "memory_search", '
            '"arguments": {"query": "x", "filter": {"tag": "user"}}}>')
    assert _extract_tool_from_text(text, TOOLS) == (
        "memory_search", {"query": "x", "filter": {"tag": "user"}})


def test_pattern5_in_middle_of_prose():

    text = (
        "Let me search memory first.\n"
        '!<function_call:{"call": "memory_search", "arguments": {"query": "API keys"}}>\n'
        "Then I'll act on the result."
    )
    assert _extract_tool_from_text(text, TOOLS) == (
        "memory_search", {"query": "API keys"})


def test_empty_text_returns_none():
    assert _extract_tool_from_text("", TOOLS) is None


def test_empty_tool_names_returns_none():
    assert _extract_tool_from_text("any text", set()) is None


def test_plain_prose_returns_none():
    assert _extract_tool_from_text(
        "Hello, just a plain sentence with no tool call.", TOOLS) is None


from agent_loop import normalize_args_for_api  # noqa: E402


def test_normalize_empty_string_becomes_object_literal():
    """The actual bug: streaming accumulated `""` for a zero-arg tool
    call. Must round-trip to a parseable `{}`."""
    assert normalize_args_for_api("") == "{}"
    assert normalize_args_for_api("   ") == "{}"
    assert normalize_args_for_api("\n") == "{}"


def test_normalize_passes_valid_json_through():
    out = normalize_args_for_api('{"port": "COM3", "baud": 9600}')

    import json
    assert json.loads(out) == {"port": "COM3", "baud": 9600}


def test_normalize_unicode_not_escaped():
    """`ensure_ascii=False` keeps form data and prompts readable. The
    canvas / memory paths routinely carry Cyrillic / CJK."""
    raw = '{"title": "Привет"}'
    out = normalize_args_for_api(raw)
    assert "Привет" in out


def test_normalize_falls_back_to_empty_on_unrepairable():
    """Truly broken arguments → `{}` rather than passing garbage to the
    next-turn API call. The executor will still see the raw string and
    surface a tool_result error."""
    assert normalize_args_for_api("not even close to JSON {{{") == "{}"


def test_normalize_uses_repair_fn_when_supplied():
    """Common trailing-comma / single-quote repairs that some models
    emit should round-trip cleanly when a repair_fn is plugged in."""
    def fake_repair(raw: str) -> str | None:

        return raw.replace("'", '"')
    out = normalize_args_for_api("{'port': 'COM3'}", fake_repair)
    import json
    assert json.loads(out) == {"port": "COM3"}


def test_normalize_returns_string_not_dict():
    """`function.arguments` MUST be a JSON STRING on the wire. Passing
    a dict here would be re-encoded by the OpenAI client but some
    providers reject anyway, and this is the contract the OpenAI tool-
    calling spec specifies."""
    out = normalize_args_for_api('{"a": 1}')
    assert isinstance(out, str)
    out_empty = normalize_args_for_api("")
    assert isinstance(out_empty, str)


def test_normalize_used_when_building_assistant_msg_tool_calls():
    """The actual fix in agent_loop.py: the assistant message's
    tool_calls[*].function.arguments goes through this helper, so
    streaming-accumulated raw arguments can never reach the
    strict-validator providers as `""` or other garbage. Pinned via a
    source-grep contract — if a future refactor reverts to raw
    `tc["arguments"]`, this test fails loud.

    (The legacy v1 loop in agent.py used to build assistant tool_call
    messages too; it was removed — agent.py now delegates all tool-call
    construction to agent_loop.run_loop, so only agent_loop.py is pinned.)"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "agent_loop.py").read_text(encoding="utf-8")


    assert "normalize_args_for_api" in src, (
        "agent_loop.py no longer routes tool_call arguments through "
        "normalize_args_for_api. This re-opens the Alibaba 400 "
        "\"function.arguments must be in JSON format\" bug."
    )


_TOOLS6 = {"browser_open", "secret_get", "shell", "write_file"}


def test_pattern6_invoke_single_param():
    text = '<invoke name="secret_get"><parameter name="key">pennygood_login</parameter></invoke>'
    assert _extract_tool_from_text(text, _TOOLS6) == ("secret_get", {"key": "pennygood_login"})


def test_pattern6_invoke_real_minimax_spacing_and_envelope():


    text = ('<minimax:tool_call> <invoke name="secret_get"> '
            '<parameter name="key">pennygood_login</parameter> </invoke> </minim')
    assert _extract_tool_from_text(text, _TOOLS6) == ("secret_get", {"key": "pennygood_login"})


def test_pattern6_invoke_multiple_params():
    text = ('<invoke name="browser_open">'
            '<parameter name="url">https://threads.com</parameter>'
            '<parameter name="visible">true</parameter></invoke>')
    assert _extract_tool_from_text(text, _TOOLS6) == (
        "browser_open", {"url": "https://threads.com", "visible": True})


def test_pattern6_url_value_not_mangled():
    text = '<invoke name="browser_open"><parameter name="url">https://a.com/x?y=1&z=2</parameter></invoke>'
    name, args = _extract_tool_from_text(text, _TOOLS6)
    assert name == "browser_open"
    assert args["url"] == "https://a.com/x?y=1&z=2"


def test_pattern6_numeric_and_json_value_coercion():
    text = ('<invoke name="shell"><parameter name="command">echo hi</parameter>'
            '<parameter name="timeout">30</parameter></invoke>')
    assert _extract_tool_from_text(text, _TOOLS6) == (
        "shell", {"command": "echo hi", "timeout": 30})


def test_pattern6_unknown_tool_returns_none():
    text = '<invoke name="not_a_tool"><parameter name="x">1</parameter></invoke>'
    assert _extract_tool_from_text(text, _TOOLS6) is None


def test_pattern6_single_quoted_attrs():
    text = "<invoke name='shell'><parameter name='command'>ls</parameter></invoke>"
    assert _extract_tool_from_text(text, _TOOLS6) == ("shell", {"command": "ls"})
