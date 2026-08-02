"""The only networked module in the service.

Everything else is pure, so this file is the single seam between deployment modes:
point ``settings.detect_url`` at the public API for the standalone demo, at the
cluster-local argus service for the bolt-on, or replace ``post_event`` with a direct
``HookDispatcher.run`` call when this folds in-tree. No caller changes either way.

Two contract details that are easy to get wrong:

* ``x-tool`` is what routes a request to the coding-agent pipeline. There is no
  ``agent_type`` field and no envelope; the body is the bare hook event.
* ``Straiker-Debug: TRUE`` is required to see ``action``/``score``. Without it the enforce
  path returns ``hookSpecificOutput.permissionDecision`` for tool events and a bare ``{}``
  for everything else, so a prompt-level block would be invisible.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import httpx

from config import Settings
from models import StraikerDetectResponse, StraikerHookEvent


class DetectClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    def _headers(self, payload: str) -> dict[str, str]:
        settings = self._settings
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.api_key}",
            "x-tool": settings.x_tool,
            "Straiker-Debug": "TRUE",
        }
        if not settings.sign_payloads:
            return headers
        timestamp = str(int(time.time()))
        signature = hmac.new(
            settings.api_key.encode(),
            f"{timestamp}.{payload}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return headers | {
            "X-Straiker-Webhook-Signature": signature,
            "X-Straiker-Webhook-Timestamp": timestamp,
        }

    async def post_event(self, event: StraikerHookEvent) -> StraikerDetectResponse | None:
        """Score one hook event. Returns None when Straiker is unreachable or errors.

        None means "no verdict", and callers fail open on it: a guardrail that cannot be
        reached must not take down the developer's session. Portkey would fail open on a
        timeout anyway, so failing open here just does it deliberately and faster.
        """
        payload = event.model_dump_json(exclude_none=True)
        try:
            response = await self._client.post(
                self._settings.detect_url,
                content=payload,
                headers=self._headers(payload),
                timeout=self._settings.detect_timeout,
            )
            response.raise_for_status()
            return StraikerDetectResponse.model_validate(response.json())
        except (httpx.HTTPError, ValueError):
            return None
