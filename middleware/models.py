"""Wire types for the Portkey webhook contract and the Straiker hook-event contract.

Two separate contracts meet in this service:

* ``PortKeyDetectRequest`` / ``PortKeyDetectResponse`` mirror what Portkey's Webhook
  guardrail posts and expects back. They are deliberately identical to the shapes argus
  already models in ``argus/argus/integrations/models.py`` so this service is a drop-in
  URL swap and, later, a drop-in ingress hop in front of argus.
* ``StraikerHookEvent`` is one reconstructed Claude Code hook event, shaped exactly as the
  native hook handler posts it, so gateway-synthesized events score through the same
  backend pipeline as endpoint-installed hooks.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

StraikerHookEventName = Literal["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]


class PortKeyRequestPart(BaseModel):
    """The request half of a Portkey webhook payload.

    ``json`` is the load-bearing field: Portkey forwards the complete, unmodified body,
    so for Anthropic ``/v1/messages`` it carries ``system``, the full ``tools`` array and
    every ``messages[].content[]`` block including ``tool_use`` / ``tool_result``.

    ``text`` is NOT usable for agentic traffic. Portkey derives it from the last message
    only, excludes the top-level ``system`` field, and Anthropic ``tool_use`` /
    ``tool_result`` blocks have no ``.text`` property, so in a tool loop it degenerates to
    empty or to the raw content array. Reading it is what breaks Claude Code scoring today.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    json_: dict[str, Any] | None = Field(default_factory=dict, alias="json")
    text: Any | None = Field(default=None)
    isStreamingRequest: bool = Field(default=False)
    isTransformed: bool | None = Field(default=False)


class PortKeyResponsePart(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    json_: dict[str, Any] | None = Field(default_factory=dict, alias="json")
    text: Any | None = Field(default=None)
    statusCode: int | None = Field(default=None)
    isTransformed: bool | None = Field(default=False)


class PortKeyDetectRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    request: PortKeyRequestPart | None = Field(default=None)
    response: PortKeyResponsePart | None = Field(default=None)
    provider: str | None = Field(default=None)
    requestType: str | None = Field(default=None)
    metadata: dict[str, Any] | None = Field(default_factory=dict)
    eventType: Literal["beforeRequestHook", "afterRequestHook"] = Field(default="beforeRequestHook")

    @property
    def request_body(self) -> dict[str, Any]:
        return (self.request.json_ or {}) if self.request else {}

    @property
    def response_body(self) -> dict[str, Any]:
        return (self.response.json_ or {}) if self.response else {}


class PortKeyDetectResponse(BaseModel):
    """Portkey reads ``verdict``; ``false`` fails the check. With ``deny: true`` on the
    guardrail that becomes a 446 and the request is killed. ``transformedData`` fully
    replaces the request or response body when present."""

    model_config = ConfigDict(populate_by_name=True)

    verdict: bool = True
    transformedData: dict[str, Any] | None = Field(default=None)


class StraikerHookEvent(BaseModel):
    """One reconstructed coding-agent hook event, shaped exactly as the native Claude Code
    hook handler posts it so both paths score through the same backend pipeline."""

    model_config = ConfigDict(extra="forbid")

    hook_event_name: StraikerHookEventName
    session_id: str
    user_name: str | None = None
    cwd: str | None = None
    model: str | None = None
    prompt: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_response: str | None = None
    tool_use_id: str | None = None
    is_error: bool | None = None
    mcp_server_name: str | None = None
    mcp_tool_name: str | None = None
    app_response: str | None = None
    stop_reason: str | None = None

    def dedup_key(self) -> str:
        """The agentic loop resends the whole transcript on every call, so request-side
        events would otherwise re-fire for the rest of the session."""
        if self.hook_event_name == "PreToolUse":
            return f"pre:{self.tool_use_id}"
        if self.hook_event_name == "PostToolUse":
            return f"post:{self.tool_use_id}"
        if self.hook_event_name == "UserPromptSubmit":
            return f"prompt:{hashlib.sha256((self.prompt or '').encode()).hexdigest()}"
        return f"stop:{hashlib.sha256((self.app_response or '').encode()).hexdigest()}"


class StraikerDetectResponse(BaseModel):
    """The debug-path response from ``/api/v1/detect``.

    ``action`` only appears when the request carries ``Straiker-Debug: TRUE``; the enforce
    path returns ``hookSpecificOutput.permissionDecision`` for tool events and a bare ``{}``
    for everything else, which is why the coding path always asks for debug.
    """

    model_config = ConfigDict(extra="allow")

    turn_id: str | None = None
    score: float | None = None
    score_category: str | None = None
    severity: str | None = None
    reason: str | None = None
    action: str | None = None

    @property
    def is_block(self) -> bool:
        return self.action == "block"
