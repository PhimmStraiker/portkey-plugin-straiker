"""Replay real Claude Code wire captures through the middleware synthesizer and diff the
result against the native hook events recorded from the same sessions.

The fixtures are the Kong plugin's corpus: each scenario was captured twice from one real
``claude -p`` run, once as raw wire traffic through a record-and-forward proxy and once as
the payloads the endpoint-installed Straiker hooks emitted. That makes them an oracle for
"would this gateway have seen what the hooks saw".

Matching mirrors the Kong harness so the numbers are comparable:

* UserPromptSubmit - exact match on the stripped prompt
* PreToolUse       - tool_name equal and tool_input a subset of the native input. Subset,
                     because the native hook fills in client-side schema defaults the model
                     never emitted (Edit's ``replace_all: false``), which is not a miss
* PostToolUse      - count based, since the native hook records no stable body to diff

Run: python3 spec/parity_check.py
"""

from __future__ import annotations

import glob
import gzip
import json
import os
import sys
from typing import Any, Iterator

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "middleware"))

import coding_agent as ca  # noqa: E402

FIXTURES = os.path.expanduser(
    "~/Projects/Straiker Projects/StraikerGateway/kong-plugin-straiker/spec/fixtures/claude-code"
)

SCOREABLE = ("UserPromptSubmit", "PreToolUse", "PostToolUse")


def _read_jsonl_gz(path: str) -> list[dict[str, Any]]:
    with gzip.open(path, "rt") as handle:
        return [record for line in handle if (record := _loads(line)) is not None]


def _loads(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def assemble_sse(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Rebuild an Anthropic message body from the streamed frames.

    A tool_use block arrives as content_block_start plus a run of input_json_delta frames,
    so the arguments only exist once the fragments are concatenated and parsed.
    """
    blocks: dict[int, dict[str, Any]] = {}
    stop_reason: str | None = None
    for event in events:
        data = event.get("data")
        if isinstance(data, str):
            data = _loads(data)
        if not isinstance(data, dict):
            continue
        kind = data.get("type")
        index = data.get("index")
        if kind == "content_block_start" and isinstance(index, int):
            blocks[index] = {**(data.get("content_block") or {}), "_json": ""}
        elif kind == "content_block_delta" and isinstance(index, int):
            block = blocks.get(index)
            delta = data.get("delta") or {}
            if block is None:
                continue
            if delta.get("type") == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif delta.get("type") == "input_json_delta":
                block["_json"] += delta.get("partial_json", "")
        elif kind == "message_delta":
            stop_reason = (data.get("delta") or {}).get("stop_reason") or stop_reason

    content = []
    for index in sorted(blocks):
        block = dict(blocks[index])
        raw_json = block.pop("_json", "")
        if block.get("type") == "tool_use":
            block["input"] = _loads(raw_json) or {} if raw_json else {}
        content.append(block)
    return {"content": content, "stop_reason": stop_reason}


def synthesize(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run one captured session through the synthesizer with cross-request dedup."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for call in calls:
        body = call.get("req_body")
        if not isinstance(body, dict) or not body.get("messages"):
            continue
        session = ca.session_id(body) or "unknown"
        parsed = ca.parse_request(body)
        if parsed.kind == "utility":
            continue
        for event in ca.request_events(parsed, session, "parity"):
            if event.dedup_key() in seen:
                continue
            seen.add(event.dedup_key())
            out.append(event.model_dump(exclude_none=True))

        sse = call.get("resp_sse")
        response_body = assemble_sse(sse) if sse else call.get("resp_body")
        if not isinstance(response_body, dict):
            continue
        for event in ca.response_events(ca.parse_response(response_body), session, "parity"):
            if event.dedup_key() in seen:
                continue
            seen.add(event.dedup_key())
            out.append(event.model_dump(exclude_none=True))
    return out


def native_events(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        payload
        for record in records
        for payload in (record.get("payload") if isinstance(record.get("payload"), dict) else record,)
        if payload.get("hook_event_name") in SCOREABLE
    ]


def _by_event(events: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [event for event in events if event.get("hook_event_name") == name]


def _is_subset(small: dict[str, Any], big: dict[str, Any]) -> bool:
    return all(key in big and big[key] == value for key, value in small.items())


def compare(mine: list[dict[str, Any]], native: list[dict[str, Any]]) -> dict[str, tuple[int, int, int]]:
    """Returns {event: (mine, native, matched)}."""
    results: dict[str, tuple[int, int, int]] = {}

    my_prompts = [e.get("prompt", "").strip() for e in _by_event(mine, "UserPromptSubmit")]
    native_prompts = [e.get("prompt", "").strip() for e in _by_event(native, "UserPromptSubmit")]
    remaining = list(my_prompts)
    matched = 0
    for prompt in native_prompts:
        if prompt in remaining:
            remaining.remove(prompt)
            matched += 1
    results["UserPromptSubmit"] = (len(my_prompts), len(native_prompts), matched)

    my_pre = _by_event(mine, "PreToolUse")
    native_pre = _by_event(native, "PreToolUse")
    pool = list(my_pre)
    matched = 0
    for want in native_pre:
        for candidate in pool:
            same_name = candidate.get("tool_name") == want.get("tool_name")
            if same_name and _is_subset(candidate.get("tool_input") or {}, want.get("tool_input") or {}):
                pool.remove(candidate)
                matched += 1
                break
    results["PreToolUse"] = (len(my_pre), len(native_pre), matched)

    my_post = _by_event(mine, "PostToolUse")
    native_post = _by_event(native, "PostToolUse")
    results["PostToolUse"] = (len(my_post), len(native_post), min(len(my_post), len(native_post)))
    return results


def scenarios() -> Iterator[tuple[str, str, str]]:
    for wire in sorted(glob.glob(os.path.join(FIXTURES, "*.wire.jsonl.gz"))):
        name = os.path.basename(wire).replace(".wire.jsonl.gz", "")
        hooks = wire.replace(".wire.jsonl.gz", ".hooks.jsonl.gz")
        if os.path.exists(hooks):
            yield name, wire, hooks


def main() -> int:
    totals: dict[str, list[int]] = {name: [0, 0, 0] for name in SCOREABLE}
    print(f"{'scenario':<24} {'UPS m/n':>10} {'Pre m/n':>10} {'Post m/n':>10}")
    print("-" * 58)
    count = 0
    for name, wire_path, hooks_path in scenarios():
        count += 1
        mine = synthesize(_read_jsonl_gz(wire_path))
        native = native_events(_read_jsonl_gz(hooks_path))
        result = compare(mine, native)
        for event, (m, n, matched) in result.items():
            totals[event][0] += m
            totals[event][1] += n
            totals[event][2] += matched
        print(
            f"{name:<24} "
            f"{result['UserPromptSubmit'][0]:>4}/{result['UserPromptSubmit'][1]:<5} "
            f"{result['PreToolUse'][0]:>4}/{result['PreToolUse'][1]:<5} "
            f"{result['PostToolUse'][0]:>4}/{result['PostToolUse'][1]:<5}"
        )

    print("-" * 58)
    print(f"\n{count} scenarios\n")
    print(f"{'event':<20} {'recall':>9} {'precision':>11}   (mine / native / matched)")
    ok = True
    for event in SCOREABLE:
        mine_n, native_n, matched = totals[event]
        recall = 100.0 * matched / native_n if native_n else 100.0
        precision = 100.0 * matched / mine_n if mine_n else 100.0
        print(f"{event:<20} {recall:>8.1f}% {precision:>10.1f}%   ({mine_n} / {native_n} / {matched})")
        if recall < 100.0:
            ok = False
    print("\nrecall is the gate: every native hook event must have been reconstructed.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
