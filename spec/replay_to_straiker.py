"""Replay real captured Claude Code sessions through the middleware and into Straiker.

This exercises the actual service code path (Portkey-shaped payload in, hook events out)
against a live tenant, so the Console fills with the same typed events a real Claude Code
session through Portkey would produce. It is the demo without needing Portkey in the loop.

The Straiker key is read from STRAIKER_API_KEY and never printed.

    export STRAIKER_API_KEY=...
    python3 spec/replay_to_straiker.py [scenario ...]
"""

from __future__ import annotations

import glob
import gzip
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "middleware"))

from fastapi.testclient import TestClient  # noqa: E402

import app as service  # noqa: E402
from parity_check import assemble_sse  # noqa: E402

FIXTURES = os.path.expanduser(
    "~/Projects/Straiker Projects/StraikerGateway/kong-plugin-straiker/spec/fixtures/claude-code"
)

USER_NAME = os.getenv("REPLAY_USER_NAME", "portkey-coding-demo")


def portkey_payload(
    request_body: dict[str, Any],
    event_type: str,
    response_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shape a webhook payload exactly as Portkey posts one for /v1/messages."""
    return {
        "request": {"json": request_body, "text": "ignored-by-design", "isStreamingRequest": False},
        "response": {
            "json": response_body or {},
            "text": "",
            "statusCode": 200 if response_body else None,
        },
        "provider": "anthropic",
        "requestType": "messages",
        "metadata": {"user_name": USER_NAME},
        "eventType": event_type,
    }


def load(path: str) -> list[dict[str, Any]]:
    with gzip.open(path, "rt") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def replay(client: TestClient, name: str, calls: list[dict[str, Any]]) -> tuple[int, int]:
    posted = blocked = 0
    for call in calls:
        body = call.get("req_body")
        if not isinstance(body, dict) or not body.get("messages"):
            continue

        before = client.post("/portkey/coding", json=portkey_payload(body, "beforeRequestHook"))
        posted += 1
        if before.json().get("verdict") is False:
            blocked += 1
            print(f"    BLOCKED on beforeRequestHook")

        sse = call.get("resp_sse")
        response_body = assemble_sse(sse) if sse else call.get("resp_body")
        if not isinstance(response_body, dict):
            continue
        after = client.post("/portkey/coding", json=portkey_payload(body, "afterRequestHook", response_body))
        posted += 1
        if after.json().get("verdict") is False:
            blocked += 1
            print(f"    BLOCKED on afterRequestHook")
    return posted, blocked


def main() -> int:
    if not os.getenv("STRAIKER_API_KEY"):
        print("STRAIKER_API_KEY is not set", file=sys.stderr)
        return 1

    wanted = sys.argv[1:]
    paths = sorted(glob.glob(os.path.join(FIXTURES, "*.wire.jsonl.gz")))
    if wanted:
        paths = [p for p in paths if os.path.basename(p).replace(".wire.jsonl.gz", "") in wanted]
    if not paths:
        print("no matching fixtures", file=sys.stderr)
        return 1

    print(f"replaying {len(paths)} session(s) as user_name={USER_NAME}")
    print(f"detect url: {service._settings.detect_url}\n")

    total_calls = total_blocked = 0
    with TestClient(service.app) as client:
        for path in paths:
            name = os.path.basename(path).replace(".wire.jsonl.gz", "")
            print(f"  {name}")
            calls, blocked = replay(client, name, load(path))
            total_calls += calls
            total_blocked += blocked

    print(f"\n{total_calls} webhook calls replayed, {total_blocked} blocked")
    print("check the Console: Defend -> the coding-agent app -> Activity")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
