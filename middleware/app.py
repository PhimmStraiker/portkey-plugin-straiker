"""Straiker coding-agent middleware for Portkey.

Portkey SaaS cannot run our code: plugins are compiled into their gateway binary. The one
customer-code extension point on SaaS is the Webhook guardrail, which is just a URL Portkey
posts to. So this service takes that URL, reconstructs Claude Code hook events from the
body Portkey forwards, and scores them through Straiker's existing ``/api/v1/detect``
coding-agent pipeline. No Portkey plugin, no argus change.

It answers the same request/response contract as argus's
``POST /api/v1/integrations/portkey/detect``, so it is a drop-in URL swap for the customer
and a drop-in ingress hop in front of argus internally.

Why not just score what argus scores today: argus reads ``request.text``, which Portkey
derives from the last message only, excludes the top-level ``system`` field, and leaves
empty when the last message is a tool block. In a Claude Code tool loop that is not the
developer's intent, which is why the normal guardrail path both false-positives and misses.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI

import coding_agent as ca
from config import Settings
from dedup import TTLDedup
from detect_client import DetectClient
from models import (
    PortKeyDetectRequest,
    PortKeyDetectResponse,
    StraikerDetectResponse,
    StraikerHookEvent,
)

logger = logging.getLogger("straiker.portkey.coding")

BLOCK_MESSAGE = "Request blocked by Straiker guardrails."

ALLOW = PortKeyDetectResponse(verdict=True)

_settings = Settings.from_env()
_dedup = TTLDedup(ttl_seconds=_settings.dedup_ttl)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with httpx.AsyncClient() as client:
        app.state.detect = DetectClient(_settings, client)
        yield


app = FastAPI(title="Straiker coding-agent middleware for Portkey", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "detect_url": _settings.detect_url,
        "x_tool": _settings.x_tool,
        "block_enabled": _settings.block_enabled,
    }


def _user_name(payload: PortKeyDetectRequest) -> str:
    """Identity must be stable for a session: the backend keys a session's event trace on
    it, so a value that changes mid-session splits one conversation into traces that never
    pair a prompt with its tool calls."""
    metadata = payload.metadata or {}
    for key in ("user_name", "end_user_id", "_user"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return _settings.default_user_name


def _anthropic_block_body(reason: str) -> dict[str, Any]:
    return {
        "id": "msg_blocked",
        "type": "message",
        "role": "assistant",
        "model": "claude",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": reason}],
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def _blocked_response(payload: PortKeyDetectRequest, reason: str | None) -> PortKeyDetectResponse:
    """Portkey kills the request on verdict false when the guardrail sets ``deny: true``.

    The transformed body is Anthropic-shaped because Claude Code speaks
    ``/v1/messages``; the OpenAI ``choices[]`` shape argus emits today would not render.
    ``reason`` is type-guarded since Straiker returns null for kill-switch style blocks.
    """
    text = reason if isinstance(reason, str) and reason else BLOCK_MESSAGE
    if payload.eventType == "afterRequestHook":
        return PortKeyDetectResponse(
            verdict=False,
            transformedData={"response": {"json": _anthropic_block_body(text)}},
        )
    return PortKeyDetectResponse(verdict=False, transformedData=None)


async def _score(events: tuple[StraikerHookEvent, ...], detect: DetectClient) -> list[StraikerDetectResponse | None]:
    if not events:
        return []
    return list(await asyncio.gather(*(detect.post_event(event) for event in events)))


def _fire_and_forget(events: tuple[StraikerHookEvent, ...], detect: DetectClient) -> None:
    """PreToolUse and Stop cannot block on Portkey SaaS: output guardrails on a streaming
    response are informational only, and Claude Code streams by default. Scoring them off
    the critical path keeps them in the Console without charging the developer for latency
    that cannot change the outcome."""
    for event in events:
        task = asyncio.create_task(detect.post_event(event))
        task.add_done_callback(lambda t: t.exception())


@app.post("/portkey/coding", response_model=PortKeyDetectResponse, response_model_exclude_none=True)
async def portkey_coding(payload: PortKeyDetectRequest) -> PortKeyDetectResponse:
    detect: DetectClient = app.state.detect
    request_body = payload.request_body

    if not request_body or not ca.is_claude_code(request_body):
        return ALLOW

    session = ca.session_id(request_body, payload.metadata)
    if not session:
        logger.info("claude code traffic without a session id; emitting nothing")
        return ALLOW

    parsed_request = ca.parse_request(request_body, chatter_filter=_settings.chatter_filter)
    if parsed_request.kind == "utility":
        return ALLOW

    user_name = _user_name(payload)

    if payload.eventType == "beforeRequestHook":
        events = ca.request_events(parsed_request, session, user_name)
        fresh = tuple(e for e in events if _dedup.claim(session, e.dedup_key()))
        verdicts = await _score(fresh, detect)
        blocking = next((v for v in verdicts if v is not None and v.is_block), None)
        if blocking and _settings.block_enabled:
            return _blocked_response(payload, blocking.reason)
        return ALLOW

    response_body = payload.response_body
    if not response_body:
        return ALLOW
    parsed_response = ca.parse_response(response_body)
    events = ca.response_events(parsed_response, session, user_name)
    fresh = tuple(e for e in events if _dedup.claim(session, e.dedup_key()))

    if payload.request and payload.request.isStreamingRequest:
        _fire_and_forget(fresh, detect)
        return ALLOW

    verdicts = await _score(fresh, detect)
    blocking = next((v for v in verdicts if v is not None and v.is_block), None)
    if blocking and _settings.block_enabled:
        return _blocked_response(payload, blocking.reason)
    return ALLOW
