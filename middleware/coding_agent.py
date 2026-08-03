"""Reconstruct Claude Code hook events from Portkey webhook traffic.

Claude Code fans one user prompt into several model calls, and the hook events an
endpoint-installed hook would emit are spread across them: the prompt arrives on the
request of the first tool-bearing call, each tool call on the response that produced it,
and each tool result on the request of the next call. This module recovers those events
from what the Portkey webhook already sees, so Straiker can score the same things the
native hooks do without a hook installed on the developer's machine.

Pure functions over already-decoded request/response bodies. No I/O, no framework
imports, no network. That is deliberate: the same module lifts unchanged into argus when
this folds in-tree, at which point only the transport around it changes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence, cast

from models import StraikerHookEvent

SCAFFOLD_PATTERN = re.compile(
    r"<(system-reminder|command-message|command-name|command-args"
    r"|local-command-stdout|local-command-caveat)>.*?</\1>",
    re.DOTALL,
)

SYSTEM_REMINDER_PREFIX = "<system-reminder>"

CLAUDE_CODE_CORE_TOOLS = frozenset({"Bash", "Read", "Edit", "TodoWrite"})

CLAUDE_CODE_SYSTEM_MARKERS = ("cc_version=", "cc_entrypoint=", "claude code")

CHATTER_MARKERS = (
    "[suggestion mode:",
    "suggest what the user might naturally type next",
    "the user stepped away and is coming back",
    "recap what you were doing in under",
    "write a 5-10 word title",
    "write the title in the predominant language",
    "you are an expert at summarizing conversations",
    "your task is to create a detailed summary of the conversation",
)

MCP_TOOL_PATTERN = re.compile(r"^mcp__(.+?)__(.+)$")

SESSION_ID_PATTERN = re.compile(r'"session_id"\s*:\s*"([^"]+)"')
LEGACY_SESSION_PATTERN = re.compile(r"session_([\w-]+)")

RequestKind = Literal["turn", "utility"]


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_use_id: str
    tool_name: str | None
    content: str
    is_error: bool


@dataclass(frozen=True, slots=True)
class ToolCall:
    tool_use_id: str
    tool_name: str
    tool_input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ParsedRequest:
    kind: RequestKind
    user_prompt: str | None
    tool_results: tuple[ToolResult, ...]
    tool_count: int
    chatter_reason: str | None


@dataclass(frozen=True, slots=True)
class ParsedResponse:
    tool_calls: tuple[ToolCall, ...]
    final_text: str | None
    stop_reason: str | None


def _as_mapping(value: object) -> Mapping[str, Any]:
    return cast("Mapping[str, Any]", value) if isinstance(value, Mapping) else {}


def _as_sequence(value: object) -> Sequence[object]:
    return cast("Sequence[object]", value) if isinstance(value, (list, tuple)) else ()


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def strip_scaffold(text: str) -> str:
    return SCAFFOLD_PATTERN.sub("", text)


def _prompt_from_string(text: str) -> str:
    """Some gateways flatten a whole turn into one string; the real prompt trails the
    last ``user:`` marker once the scaffolding wrappers are removed."""
    residue = strip_scaffold(text)
    return re.split(r"\nuser:", residue)[-1].strip()


def _text_blocks(content: object) -> tuple[str, ...]:
    return tuple(
        text
        for block in _as_sequence(content)
        if (mapping := _as_mapping(block)).get("type") == "text"
        and (text := _as_str(mapping.get("text"))) is not None
    )


def _system_text(system: object) -> str:
    if isinstance(system, str):
        return system
    return "\n".join(_text_blocks(system))


def _tool_names(request_body: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        name
        for tool in _as_sequence(request_body.get("tools"))
        for mapping in (_as_mapping(tool),)
        if (name := _as_str(mapping.get("name")) or _as_str(_as_mapping(mapping.get("function")).get("name")))
    )


def is_claude_code(request_body: Mapping[str, Any]) -> bool:
    """Two signals, either sufficient.

    Portkey strips request headers from webhook payloads, so the ``claude-cli/`` user
    agent the in-process integrations rely on is not available here; detection is limited
    to the body. The billing-header markers in the first system block (``cc_version=`` /
    ``cc_entrypoint=``) are the current client's fingerprint, and the core tool set is
    present on every main turn.
    """
    if not CLAUDE_CODE_CORE_TOOLS.isdisjoint(_tool_names(request_body)):
        return True
    system = _system_text(request_body.get("system")).lower()
    return any(marker in system for marker in CLAUDE_CODE_SYSTEM_MARKERS)


def session_id(request_body: Mapping[str, Any], fallback_metadata: Mapping[str, Any] | None = None) -> str | None:
    """Claude Code carries a stable session id inside ``metadata.user_id``, which is a
    JSON *string* holding ``{"device_id": ..., "session_id": ...}``.

    It is constant across every call of a session, which is what lets the backend pair a
    prompt with the tool calls it produced. Without one, emitting events would fragment
    that trace, so callers should emit nothing rather than invent an id.
    """
    user_id = _as_str(_as_mapping(request_body.get("metadata")).get("user_id"))
    if user_id:
        try:
            decoded = json.loads(user_id)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, Mapping):
            if parsed := _as_str(decoded.get("session_id")):
                return parsed
        if match := SESSION_ID_PATTERN.search(user_id):
            return match.group(1)
        if match := LEGACY_SESSION_PATTERN.search(user_id):
            return match.group(1)
    meta = _as_mapping(fallback_metadata)
    return _as_str(meta.get("session_id")) or _as_str(meta.get("_session_id"))


def _tool_use_pair(block: object) -> tuple[str, str] | None:
    mapping = _as_mapping(block)
    if mapping.get("type") != "tool_use":
        return None
    tool_use_id = _as_str(mapping.get("id"))
    name = _as_str(mapping.get("name"))
    return (tool_use_id, name) if tool_use_id and name else None


def _tool_call_pair(call: object) -> tuple[str, str] | None:
    mapping = _as_mapping(call)
    tool_use_id = _as_str(mapping.get("id"))
    name = _as_str(_as_mapping(mapping.get("function")).get("name"))
    return (tool_use_id, name) if tool_use_id and name else None


def _assistant_tool_uses(message: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    pairs = (
        *(_tool_use_pair(block) for block in _as_sequence(message.get("content"))),
        *(_tool_call_pair(call) for call in _as_sequence(message.get("tool_calls"))),
    )
    return tuple(pair for pair in pairs if pair is not None)


def _tool_names_by_id(messages: Sequence[object]) -> Mapping[str, str]:
    """A ``tool_result`` names only the id it answers, and the ``tool_use`` that produced
    that id came from a response this service no longer holds. The transcript is resent in
    full on every call, so the name is recoverable from the current request."""
    return {
        tool_use_id: name
        for message in messages
        for mapping in (_as_mapping(message),)
        if mapping.get("role") == "assistant"
        for tool_use_id, name in _assistant_tool_uses(mapping)
    }


def _tail_after_last_assistant(messages: Sequence[object]) -> Sequence[object]:
    last_assistant = max(
        (index for index, message in enumerate(messages) if _as_mapping(message).get("role") == "assistant"),
        default=-1,
    )
    return messages[last_assistant + 1 :]


def _block_text(content: object) -> str:
    if isinstance(content, str):
        return content
    texts = tuple(
        text
        for block in _as_sequence(content)
        for mapping in (_as_mapping(block),)
        if (text := _as_str(mapping.get("text"))) is not None
    )
    return "\n".join(texts)


def _tool_results(tail: Sequence[object], names: Mapping[str, str]) -> tuple[ToolResult, ...]:
    anthropic = tuple(
        ToolResult(
            tool_use_id=tool_use_id,
            tool_name=names.get(tool_use_id),
            content=_block_text(block_mapping.get("content")),
            is_error=bool(block_mapping.get("is_error")),
        )
        for message in tail
        for message_mapping in (_as_mapping(message),)
        if message_mapping.get("role") == "user"
        for block in _as_sequence(message_mapping.get("content"))
        for block_mapping in (_as_mapping(block),)
        if block_mapping.get("type") == "tool_result" and (tool_use_id := _as_str(block_mapping.get("tool_use_id")))
    )
    openai = tuple(
        ToolResult(
            tool_use_id=tool_use_id,
            tool_name=names.get(tool_use_id) or _as_str(message_mapping.get("name")),
            content=_block_text(message_mapping.get("content")),
            is_error=False,
        )
        for message in tail
        for message_mapping in (_as_mapping(message),)
        if message_mapping.get("role") == "tool" and (tool_use_id := _as_str(message_mapping.get("tool_call_id")))
    )
    return anthropic + openai


def _user_prompt(tail: Sequence[object]) -> str | None:
    user_contents = tuple(
        mapping.get("content")
        for message in tail
        for mapping in (_as_mapping(message),)
        if mapping.get("role") == "user"
    )
    if not user_contents:
        return None
    content = user_contents[-1]
    if isinstance(content, str):
        return _prompt_from_string(content) or None
    candidates = tuple(text for text in _text_blocks(content) if not text.lstrip().startswith(SYSTEM_REMINDER_PREFIX))
    return candidates[-1].strip() if candidates else None


def _chatter_reason(tool_count: int, prompt: str | None) -> str | None:
    """Zero tools is the structural signal: main turns always carry the full tool set,
    while title generation, suggestion mode and recaps carry none. It is roughly a quarter
    of all Claude Code calls and scoring it is what produces detections on traffic the
    developer never wrote."""
    if tool_count == 0:
        return "no_tools_utility"
    haystack = (prompt or "").lower()
    return next((f"marker:{marker}" for marker in CHATTER_MARKERS if marker in haystack), None)


def parse_request(request_body: Mapping[str, Any], chatter_filter: bool = True) -> ParsedRequest:
    """Classify one model call and pull out the prompt and any tool results it carries."""
    messages = _as_sequence(request_body.get("messages"))
    tail = _tail_after_last_assistant(messages)
    prompt = _user_prompt(tail)
    tool_count = len(_tool_names(request_body))
    reason = _chatter_reason(tool_count, prompt) if chatter_filter else None
    return ParsedRequest(
        kind="utility" if reason else "turn",
        user_prompt=prompt,
        tool_results=_tool_results(tail, _tool_names_by_id(messages)),
        tool_count=tool_count,
        chatter_reason=reason,
    )


def _tool_arguments(function: Mapping[str, Any]) -> dict[str, Any]:
    arguments = function.get("arguments")
    if isinstance(arguments, Mapping):
        return dict(_as_mapping(arguments))
    if not isinstance(arguments, str) or not arguments:
        return {}
    try:
        decoded = json.loads(arguments)
    except json.JSONDecodeError:
        return {"arguments": arguments}
    return dict(_as_mapping(decoded)) if isinstance(decoded, Mapping) else {"arguments": decoded}


def parse_response(response_body: Mapping[str, Any]) -> ParsedResponse:
    """Read tool calls and the final answer off a response, in either dialect.

    Anthropic ``/v1/messages`` returns ``content[]`` with ``tool_use`` blocks and a
    top-level ``stop_reason``; OpenAI returns ``choices[0].message.tool_calls`` with a
    ``finish_reason``. Portkey passes the upstream shape through, so both occur depending
    on which endpoint the client used.
    """
    content = response_body.get("content")
    if isinstance(content, (list, tuple)):
        anthropic_calls = tuple(
            ToolCall(
                tool_use_id=tool_use_id,
                tool_name=name,
                tool_input=dict(_as_mapping(mapping.get("input"))),
            )
            for block in content
            for mapping in (_as_mapping(block),)
            if mapping.get("type") == "tool_use"
            and (tool_use_id := _as_str(mapping.get("id")))
            and (name := _as_str(mapping.get("name")))
        )
        return ParsedResponse(
            tool_calls=anthropic_calls,
            final_text=_block_text(content) or None,
            stop_reason=_as_str(response_body.get("stop_reason")),
        )

    choices = _as_sequence(response_body.get("choices"))
    if not choices:
        return ParsedResponse(tool_calls=(), final_text=None, stop_reason=None)
    choice = _as_mapping(choices[0])
    message = _as_mapping(choice.get("message"))
    openai_calls = tuple(
        ToolCall(
            tool_use_id=tool_use_id,
            tool_name=name,
            tool_input=_tool_arguments(function),
        )
        for call in _as_sequence(message.get("tool_calls"))
        for mapping in (_as_mapping(call),)
        for function in (_as_mapping(mapping.get("function")),)
        if (tool_use_id := _as_str(mapping.get("id"))) and (name := _as_str(function.get("name")))
    )
    return ParsedResponse(
        tool_calls=openai_calls,
        final_text=_as_str(message.get("content")),
        stop_reason=_as_str(choice.get("finish_reason")),
    )


def _mcp_fields(tool_name: str) -> tuple[str | None, str | None]:
    match = MCP_TOOL_PATTERN.match(tool_name)
    return (match.group(1), match.group(2)) if match else (None, None)


def request_events(
    parsed: ParsedRequest, session: str, user_name: str | None
) -> tuple[StraikerHookEvent, ...]:
    """Emitted in native order: tool results from the previous turn, then the prompt.

    ``PostToolUse`` arriving here rather than on the response is what makes the gateway
    position stronger than the endpoint hook: a poisoned tool result can be blocked before
    it ever reaches the model.
    """
    post_tool_use = tuple(
        StraikerHookEvent(
            hook_event_name="PostToolUse",
            session_id=session,
            user_name=user_name,
            tool_name=result.tool_name,
            tool_use_id=result.tool_use_id,
            tool_response=result.content,
            is_error=result.is_error,
        )
        for result in parsed.tool_results
    )
    if parsed.kind != "turn" or not parsed.user_prompt:
        return post_tool_use
    return post_tool_use + (
        StraikerHookEvent(
            hook_event_name="UserPromptSubmit",
            session_id=session,
            user_name=user_name,
            prompt=parsed.user_prompt,
        ),
    )


def response_events(
    parsed: ParsedResponse, session: str, user_name: str | None
) -> tuple[StraikerHookEvent, ...]:
    """PreToolUse for every tool the model wants to run, then Stop for a final answer.

    Stop has no native equivalent: the endpoint hook never sees the model's answer, but a
    gateway does, so it is emitted for telemetry and output-side scoring only.
    """
    pre_tool_use = tuple(
        StraikerHookEvent(
            hook_event_name="PreToolUse",
            session_id=session,
            user_name=user_name,
            tool_name=call.tool_name,
            tool_use_id=call.tool_use_id,
            tool_input=call.tool_input,
            mcp_server_name=mcp_server,
            mcp_tool_name=mcp_tool,
        )
        for call in parsed.tool_calls
        for mcp_server, mcp_tool in (_mcp_fields(call.tool_name),)
    )
    if parsed.tool_calls or not parsed.final_text:
        return pre_tool_use
    return pre_tool_use + (
        StraikerHookEvent(
            hook_event_name="Stop",
            session_id=session,
            user_name=user_name,
            app_response=parsed.final_text,
            stop_reason=parsed.stop_reason,
        ),
    )
