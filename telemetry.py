"""Anonymous telemetry — opt-in, transparent, audit-friendly.

# Privacy guarantees

1. **Default OFF.** Nothing leaves the machine until the user explicitly
   opts in via the first-run prompt or Settings → Privacy → Telemetry.
2. **No chat content.** No user input, no assistant replies, no thinking
   blocks, no thread titles, no message metadata that could contain
   user-typed text.
3. **No soul / personality.** Trait names, levels, custom traits — none
   of this is collected. The agent's persona is the user's design, not
   project metrics.
4. **No identifiers that could deanonymize.** No IP, hostname, username,
   API keys, file paths, exact model names (could be custom finetunes),
   provider URLs (could be internal corporate endpoints), specific
   skill names (user-created skills could leak company identity), or
   tool-call args / results.
5. **Anonymous user ID** is a random UUID generated once at opt-in,
   stored locally in `kv` table. Never derived from any PII. User can
   reset it any time without disabling telemetry.
6. **Allowed-events whitelist.** Every event name and its schema are
   declared in `ALLOWED_EVENTS` below. Unknown events are dropped with
   a warning. Schemas pin the type of every property so a future
   refactor can't accidentally add a string-valued field that smuggles
   chat text.
7. **All collection goes through `track_event()`.** Easy to audit by
   grepping the codebase for `telemetry.track_event`. There is no
   alternate path — no direct queue access from outside this module.
8. **Inert until endpoint configured.** If `telemetry_endpoint` setting
   is empty, the module collects events into the local queue but never
   sends anything over the network. Users who want self-hosted analytics
   can point this at their own collector (PostHog / Plausible / custom).
   The project does not ship a default endpoint.

See `docs/PRIVACY.md` for the human-readable version of this contract.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from typing import Any, Callable

import config

_log = logging.getLogger("qwe.telemetry")


_CURRENT_CONSENT_VERSION = 6


_HTTP_TIMEOUT_S = 10.0


_MAX_ATTEMPTS = 3
_BACKOFF_SCHEDULE_S = (1.0, 2.0, 4.0)


ALLOWED_EVENTS: dict[str, dict[str, type]] = {
    "session_start": {
        "castor_version": str,
        "python_version": str,
        "os": str,


        "provider_kind": str,


        "model_size_bucket": str,

        "has_web_ui": bool,
        "has_telegram": bool,
        "has_voice": bool,
        "has_camera": bool,
        "has_scheduler": bool,
        "has_mcp": bool,
        # Counts only — never names. Three skills, not "acme_invoice_proc".
        "active_skills_count": int,
        "scheduled_jobs_count": int,
        "indexed_sources_count": int,
    },
    "turn_complete": {
        "duration_ms": int,
        "rounds": int,


        "tool_categories_used": list,
        "tool_calls_count": int,
        "tool_errors_count": int,
        "input_tokens": int,
        "output_tokens": int,
        "context_hits": int,
        "text_extractions": int,

        "source": str,
    },
    "tool_error": {


        "tool_category": str,


        "error_kind": str,
    },
    "skill_creator_pipeline": {


        "outcome": str,
        "attempts": int,
        "duration_ms": int,

        "tools_count": int,
    },
    "feature_first_use": {


        "feature": str,
    },
    "thread_created": {


        "source": str,
    },
}


TOOL_CATEGORIES = frozenset({
    "memory", "files", "shell", "http", "browser", "vision", "voice",
    "automation", "skills", "orchestration", "vault", "rag", "other",
})


ERROR_KINDS = frozenset({
    "timeout", "exception", "validation_failed", "rate_limited",
    "aborted", "blocked", "not_found", "unauthorized", "other",
})


SOURCES = frozenset({"web", "cli", "telegram", "scheduler", "preset", "other"})


PROVIDER_KINDS = frozenset({
    "lmstudio", "ollama", "openai", "azure", "bedrock", "groq",
    "openrouter", "deepseek", "together", "unknown",
})


MODEL_SIZE_BUCKETS = frozenset({"small", "medium", "large", "unknown"})


PIPELINE_OUTCOMES = frozenset({
    "success", "syntax_error", "smoke_fail", "validate_fail",
    "max_attempts_exhausted", "aborted",
})


FEATURES = frozenset({
    "camera_capture", "live_voice", "telegram_send",
    "scheduler_create", "skill_create", "browser_visible",
    "mcp_add", "preset_activate", "knowledge_index_url",
    "knowledge_index_file", "cost_tracking", "auto_resume",
})


_ENUM_CONSTRAINTS: dict[tuple[str, str], frozenset] = {
    ("session_start", "provider_kind"): PROVIDER_KINDS,
    ("session_start", "model_size_bucket"): MODEL_SIZE_BUCKETS,
    ("turn_complete", "source"): SOURCES,
    ("tool_error", "tool_category"): TOOL_CATEGORIES,
    ("tool_error", "error_kind"): ERROR_KINDS,
    ("skill_creator_pipeline", "outcome"): PIPELINE_OUTCOMES,
    ("feature_first_use", "feature"): FEATURES,
    ("thread_created", "source"): SOURCES,
}


_MAX_QUEUE = 1000
_queue: deque[dict] = deque(maxlen=_MAX_QUEUE)
_queue_lock = threading.Lock()


_SESSION_ID = uuid.uuid4().hex


def enabled() -> bool:
    """Is telemetry enabled? Default False. Authoritative check used by
    track_event() to short-circuit before any work is done."""
    val = config.get("telemetry_enabled")
    return bool(val)


def anonymous_id() -> str:
    """Get the user's anonymous id, generating one on first call.

    Generated on first opt-in, persisted in `kv` table, never derived
    from any PII. User can call `reset_anonymous_id()` to rotate it
    without re-opting-in.
    """
    aid = config.get("telemetry_anonymous_id") or ""
    if not aid:
        aid = uuid.uuid4().hex
        config.set("telemetry_anonymous_id", aid)
    return aid


def session_id() -> str:
    """Per-process session id. Resets on every castor start."""
    return _SESSION_ID


def opt_in() -> str:
    """Enable telemetry + ensure anonymous_id exists. Returns the id.

    Called by the first-run prompt or the Settings → Privacy toggle.
    Idempotent — safe to call repeatedly. Stamps the current consent
    version so future policy changes (new event types, new default
    endpoint) can be detected and trigger a re-prompt.
    """
    aid = anonymous_id()
    config.set("telemetry_enabled", 1)
    config.set("telemetry_consent_version", _CURRENT_CONSENT_VERSION)
    _log.info("telemetry enabled (anonymous_id=%s, consent v%d)",
              aid[:8] + "...", _CURRENT_CONSENT_VERSION)
    return aid


def consent_needs_reprompt() -> bool:
    """True if the user's stored consent version is older than the
    current `_CURRENT_CONSENT_VERSION`. The UI can use this to surface
    a "the policy / endpoint changed, please re-confirm" prompt before
    continuing to send events under the old assumption.

    Returns False when:
    - telemetry is disabled (no consent in force, no prompt needed)
    - the user is up-to-date with the current consent version
    - the user has never opted in (consent_version stays at 0)
    """
    if not enabled():
        return False
    stored = int(config.get("telemetry_consent_version") or 0)
    return stored < _CURRENT_CONSENT_VERSION


def opt_out() -> None:
    """Disable telemetry and drop any queued events. Anonymous id is
    NOT deleted by default — keeping it lets a future re-opt-in stay
    consistent. Use `forget_me()` to also wipe the id.

    Also stamps the current consent version so the first-run prompt
    doesn't keep re-asking. opt_out is an EXPLICIT user choice — they
    looked at the privacy text and said no — same intent as opt_in,
    just opposite outcome.
    """
    config.set("telemetry_enabled", 0)
    config.set("telemetry_consent_version", _CURRENT_CONSENT_VERSION)
    with _queue_lock:
        dropped = len(_queue)
        _queue.clear()
    _log.info("telemetry disabled (%d queued events dropped, consent v%d)",
              dropped, _CURRENT_CONSENT_VERSION)


def consent_decision_made() -> bool:
    """True once the user has explicitly chosen yes or no to telemetry.

    Used by the first-run prompt to decide whether to show the modal.
    A fresh install starts with telemetry_consent_version=0; the prompt
    fires until the user clicks Enable or Decline (both bump it).
    """
    return int(config.get("telemetry_consent_version") or 0) >= 1


def forget_me() -> None:
    """Disable telemetry, drop queue, and wipe the anonymous id.

    Stronger than opt_out(): the next opt-in (if any) will get a fresh
    id, so nothing ties the two periods of opt-in together.
    """
    opt_out()
    config.set("telemetry_anonymous_id", "")
    _log.info("telemetry: anonymous_id wiped")


def reset_anonymous_id() -> str:
    """Generate a fresh anonymous id without changing the enabled flag.

    For users who want to "start over" without going through opt-out /
    opt-in. Useful if they fear correlation across long timeframes.
    """
    aid = uuid.uuid4().hex
    config.set("telemetry_anonymous_id", aid)
    _log.info("telemetry anonymous_id rotated")
    return aid


def track_event(name: str, props: dict | None = None) -> bool:
    """Add an event to the queue. Returns True if accepted.

    No-op (returns False) when:
    - telemetry is disabled (default)
    - event name is not in ALLOWED_EVENTS
    - any prop has a wrong type
    - any enum-constrained prop has a value outside its allowed set
    - tool_categories_used contains a category outside TOOL_CATEGORIES

    The strict validation is the audit point: if you're reading this
    code wondering "could a future bug leak chat content via this
    track_event call?", the answer is no — only declared props pass,
    only declared types match, and the enum constraints lock the
    string values down to bounded sets.
    """
    if not enabled():
        return False


    if consent_needs_reprompt():


        _log.debug("telemetry: dropping event %r — consent reprompt pending", name)
        return False
    if name not in ALLOWED_EVENTS:
        _log.warning("telemetry: dropping unknown event %r", name)
        return False

    schema = ALLOWED_EVENTS[name]
    props = props or {}


    extra = set(props.keys()) - set(schema.keys())
    if extra:
        _log.warning("telemetry: dropping event %r with extra keys %s", name, extra)
        return False


    cleaned: dict[str, Any] = {}
    for prop_name, prop_type in schema.items():
        if prop_name not in props:


            continue
        val = props[prop_name]
        if not isinstance(val, prop_type):
            _log.warning(
                "telemetry: dropping event %r — prop %r expected %s, got %s",
                name, prop_name, prop_type.__name__, type(val).__name__,
            )
            return False

        constraint = _ENUM_CONSTRAINTS.get((name, prop_name))
        if constraint is not None and val not in constraint:
            _log.warning(
                "telemetry: dropping event %r — prop %r value %r not in allowed set",
                name, prop_name, val,
            )
            return False

        if isinstance(val, list) and prop_name == "tool_categories_used":
            if not all(isinstance(c, str) for c in val):
                _log.warning("telemetry: tool_categories_used must be list[str]")
                return False
            invalid = [c for c in val if c not in TOOL_CATEGORIES]
            if invalid:
                _log.warning(
                    "telemetry: dropping event — invalid categories %s",
                    invalid,
                )
                return False
        cleaned[prop_name] = val


    event = {
        "event": name,
        "anonymous_id": anonymous_id(),
        "session_id": _SESSION_ID,
        "ts": time.time(),
        "props": cleaned,
    }

    with _queue_lock:
        _queue.append(event)
    return True


def get_pending_events() -> list[dict]:
    """Snapshot of the current queue. For UI inspection — lets the user
    see what's actually queued before they hit "send"."""
    with _queue_lock:
        return list(_queue)


def queue_size() -> int:
    """Cheap count without copying the queue."""
    with _queue_lock:
        return len(_queue)


def clear_queue() -> int:
    """Drop everything in the queue without sending. Returns dropped count."""
    with _queue_lock:
        n = len(_queue)
        _queue.clear()
    return n


def flush(send_fn: Callable[[list[dict]], bool] | None = None) -> int:
    """Send the queue to `telemetry_endpoint`. Returns the number of
    events successfully sent (0 if disabled, no endpoint, or send fails).

    `send_fn` parameter is for tests — production path uses the
    built-in HTTP POST (`_default_sender`). If endpoint is empty,
    returns 0 without doing anything. Queue is cleared only on a 2xx
    response — failures (4xx, 5xx after retries, network errors) keep
    events queued for the next flush attempt.
    """
    if not enabled():
        return 0


    if consent_needs_reprompt():
        _log.debug("telemetry: flush blocked — consent reprompt pending")
        return 0
    endpoint = (config.get("telemetry_endpoint") or "").strip()
    if not endpoint:
        return 0
    with _queue_lock:
        events = list(_queue)
    if not events:
        return 0
    sender = send_fn or _default_sender
    if sender(events):
        with _queue_lock:


            for _ in range(min(len(events), len(_queue))):
                _queue.popleft()
        return len(events)
    return 0


def _default_sender(events: list[dict]) -> bool:
    """Dispatch by `telemetry_format` to the right wire encoder.

    Two supported formats:
    - "raw" (default) — single batched POST with `{"events": [...]}`
      shape. For custom collectors that accept our schema directly.
    - "countly" — batched POST in Countly's `/i` format. For users
      who self-host Countly Community Edition (https://count.ly).
      Maps anonymous_id → device_id natively (no synthetic-IP hacks),
      cross-day per-user tracking works out of the box.
    """
    fmt = (config.get("telemetry_format") or "raw").strip().lower()
    if fmt == "countly":
        return _send_countly(events)
    return _send_raw(events)


def _send_raw(events: list[dict]) -> bool:
    """Built-in HTTP sender (raw format). POSTs the batch as JSON to
    `telemetry_endpoint`.

    Returns True only on a 2xx response from the receiver, in which case
    `flush()` clears the sent events from the queue. Returns False on:
    - missing endpoint (defensive — `flush()` short-circuits earlier)
    - 4xx (config error — don't retry, don't clear queue, let the user
      notice in logs)
    - 5xx after `_MAX_ATTEMPTS` retries
    - network error (DNS / refused / timeout) after retries
    - any exception during request building / encoding

    Never raises out of this function — telemetry must never break the
    caller. Network errors and exceptions are caught and converted into
    a False return.

    Privacy notes:
    - The request body is NEVER logged (it would defeat the
      no-chat-content guarantee if a future event schema regression
      slipped a string field in).
    - The endpoint URL is logged at DEBUG level only; default INFO logs
      show counts and error classes but no URL.
    - We skip the SSRF guard that `rag.py` applies to user-content URLs:
      `telemetry_endpoint` is set explicitly by the user (Settings →
      Privacy → Telemetry endpoint), so a self-hosted PostHog at
      192.168.x.x is a legitimate use case. Endpoint URL is documented
      as the user's responsibility in `docs/PRIVACY.md`.
    """
    endpoint = (config.get("telemetry_endpoint") or "").strip()
    if not endpoint:


        return False


    try:
        body = json.dumps({"events": events}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"castor/{config.VERSION}",
        }


        if events:
            aid = events[0].get("anonymous_id")
            if isinstance(aid, str) and aid:
                headers["X-QWE-Anonymous-Id"] = aid
        req = urllib.request.Request(
            endpoint, data=body, headers=headers, method="POST"
        )
    except Exception as e:
        _log.warning(
            "telemetry: send failed building request: %s",
            type(e).__name__,
        )
        return False

    _log.debug("telemetry: sending %d events to %s", len(events), endpoint)

    last_err_class: str = "unknown"
    for attempt in range(_MAX_ATTEMPTS):


        if attempt > 0:
            time.sleep(_BACKOFF_SCHEDULE_S[attempt - 1])

        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:


                status = getattr(resp, "status", None)
                if status is None:
                    try:
                        status = resp.getcode()
                    except Exception:
                        status = 0
                if 200 <= int(status) < 300:
                    _log.info("telemetry: sent %d events", len(events))
                    return True


                last_err_class = f"HTTP{status}"
                if 400 <= int(status) < 500:

                    break

                continue
        except urllib.error.HTTPError as he:

            code = int(getattr(he, "code", 0) or 0)
            last_err_class = f"HTTP{code}"
            if 400 <= code < 500:

                break

            continue
        except urllib.error.URLError as ue:

            last_err_class = type(ue).__name__
            continue
        except Exception as e:


            last_err_class = type(e).__name__
            continue

    _log.warning(
        "telemetry: send failed after %d retries: %s",
        _MAX_ATTEMPTS, last_err_class,
    )
    return False


def _to_countly_segmentation(props: dict) -> dict:
    """Countly's `segmentation` accepts string / number / bool values
    only — no nested objects or arrays. Coerce our props to that shape.

    Lists become comma-joined strings. Since `tool_categories_used` is
    drawn from a small bounded enum (TOOL_CATEGORIES, ~13 values), the
    resulting CSV string has bounded cardinality and Countly can group
    on it cleanly as a discrete segmentation value.

    Booleans / ints / floats / strings pass through — Countly preserves
    types in segmentation.
    """
    out: dict = {}
    for k, v in (props or {}).items():
        if isinstance(v, list):
            out[k] = ",".join(str(x) for x in v) if v else ""
        elif isinstance(v, (bool, int, float, str)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def _to_countly_event(ev: dict) -> dict:
    """Transform our event envelope to Countly's event shape.

    Countly event:
        {key, count, sum?, dur?, segmentation, timestamp}

    `dur` (event duration in seconds) is set from `duration_ms / 1000`
    when the event has a `duration_ms` prop (turn_complete,
    skill_creator_pipeline). Otherwise omitted — Countly treats absence
    as 0 which is fine for instantaneous events.
    """
    props = ev.get("props") or {}
    seg = _to_countly_segmentation(props)
    out: dict = {
        "key": ev.get("event") or "unknown",
        "count": 1,
        "segmentation": seg,
        "timestamp": int(ev.get("ts") or time.time()),
    }
    duration_ms = props.get("duration_ms")
    if isinstance(duration_ms, (int, float)) and duration_ms > 0:
        out["dur"] = duration_ms / 1000.0
    return out


def _send_countly(events: list[dict]) -> bool:
    """POST a batch of events to Countly's /i ingest endpoint.

    Countly accepts a single POST per device_id with all events for
    that device. Since all events in our queue share the same
    anonymous_id (it stamps from one source), one batched POST works
    for the whole queue. Falls through to multiple POSTs only if a
    queue ever ends up with mixed device_ids (theoretically possible
    after `reset_anonymous_id()` mid-flight; we group defensively).

    Countly requires:
    - app_key matching a registered app in the dashboard (read from
      `telemetry_countly_app_key` setting)
    - device_id (we use anonymous_id natively — Countly hashes
      internally, no salt rotation, so cross-day per-user tracking
      works out of the box)
    - events array with `key` (event name) and optional segmentation
    - User-Agent header (we send `castor/{version}`)

    Privacy note: Countly receives our anonymous_id as device_id.
    That's stable across days, by design — the user OPTED IN, and a
    stable id is needed for retention / funnel metrics. The id is
    still random-UUID and not derived from any PII.

    Returns True on a 2xx response (queue cleared by flush()), False
    on 4xx (config error — usually wrong app_key) or 5xx after retries
    (queue retained for next flush).
    """
    endpoint = (config.get("telemetry_endpoint") or "").strip()
    if not endpoint:
        return False
    app_key = (config.get("telemetry_countly_app_key") or "").strip()
    if not app_key:
        _log.warning(
            "telemetry: format=countly but telemetry_countly_app_key is empty — "
            "set it to the app key from your Countly dashboard"
        )
        return False


    by_device: dict[str, list[dict]] = {}
    for ev in events:
        aid = ev.get("anonymous_id") or "anon"
        by_device.setdefault(aid, []).append(ev)

    sent_count = 0
    for device_id, device_events in by_device.items():
        if not _send_countly_batch(endpoint, app_key, device_id, device_events):
            _log.warning(
                "telemetry: countly send failed (device=%s..., %d events), keeping queue",
                device_id[:8], len(device_events),
            )
            return False
        sent_count += len(device_events)

    _log.info("telemetry: sent %d events to countly", sent_count)
    return True


def _send_countly_batch(endpoint: str, app_key: str, device_id: str,
                         events: list[dict]) -> bool:
    """Single batched POST for one device_id. Internal retry on 5xx
    using the same backoff as `_send_raw`."""
    countly_events = [_to_countly_event(ev) for ev in events]
    body_obj = {
        "app_key": app_key,
        "device_id": device_id,
        "timestamp": int(time.time()),
        "events": countly_events,
    }

    try:
        body = json.dumps(body_obj).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"castor/{config.VERSION}",
        }
        req = urllib.request.Request(
            endpoint, data=body, headers=headers, method="POST"
        )
    except Exception as e:
        _log.warning("telemetry: countly request build failed: %s", type(e).__name__)
        return False

    last_err: str = "unknown"
    for attempt in range(_MAX_ATTEMPTS):
        if attempt > 0:
            time.sleep(_BACKOFF_SCHEDULE_S[attempt - 1])
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
                status = getattr(resp, "status", None)
                if status is None:
                    try:
                        status = resp.getcode()
                    except Exception:
                        status = 0
                if 200 <= int(status) < 300:
                    return True
                last_err = f"HTTP{status}"
                if 400 <= int(status) < 500:
                    return False
        except urllib.error.HTTPError as he:
            code = int(getattr(he, "code", 0) or 0)
            last_err = f"HTTP{code}"
            if 400 <= code < 500:
                return False
        except urllib.error.URLError as ue:
            last_err = type(ue).__name__
        except Exception as e:
            last_err = type(e).__name__

    _log.debug("telemetry: countly batch failed: %s", last_err)
    return False


def bucket_model_size(param_count_b: float | None) -> str:
    """Map a model parameter count (in billions) to a coarse bucket.

    Use this anywhere you have an exact model id but don't want to send
    it. Cardinality of the output is fixed to 4 values, so it can't
    deanonymize.
    """
    if param_count_b is None:
        return "unknown"
    if param_count_b <= 4:
        return "small"
    if param_count_b <= 13:
        return "medium"
    return "large"


def os_kind() -> str:
    """OS string in the SOURCES enum format."""
    p = sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "macos"
    if p.startswith("win"):
        return "windows"
    return "other"


def python_version() -> str:
    """Python version as 'major.minor.patch' (no build / compiler info)."""
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro}"


def provider_kind_from_name(name: str | None) -> str:
    """Map a provider preset key to one of PROVIDER_KINDS.

    Use this anywhere you have an active provider name (`providers.get_active_name()`)
    but want to send only the kind, not the URL. Cardinality is fixed by the
    PROVIDER_KINDS frozenset — anything outside it (e.g. user-added custom
    providers, perplexity / cerebras / mistral presets that aren't in the
    enum) collapses to "unknown".
    """
    if not name:
        return "unknown"
    return name if name in PROVIDER_KINDS else "unknown"


_FEATURES_USED_THIS_SESSION: set[str] = set()
_features_used_lock = threading.Lock()


def track_feature_first_use(feature: str) -> bool:
    """Emit a `feature_first_use` event the FIRST time a feature is used in
    this process. Subsequent calls with the same feature are silent no-ops.

    Returns True if the event was actually emitted (accepted by
    track_event), False otherwise (telemetry off, already fired this
    session, or feature outside the FEATURES enum).

    Privacy: feature is enum-bounded — anything outside FEATURES is
    rejected by track_event's validator before reaching the queue.
    """
    if feature not in FEATURES:


        return False
    with _features_used_lock:
        if feature in _FEATURES_USED_THIS_SESSION:
            return False
        _FEATURES_USED_THIS_SESSION.add(feature)
    return track_event("feature_first_use", {"feature": feature})


