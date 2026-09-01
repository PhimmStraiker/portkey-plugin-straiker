"""Run the Portkey coding-agent contract suite against Straiker.

Posts Portkey's own guardrail-webhook envelope to /api/v1/detect with
``x-tool: portkey-claude-code``, which is byte-identical to what a Portkey webhook
guardrail sends. Measures what Straiker actually detects, without spending model
tokens on 133 round trips through a provider.

Each case gets its own session id so replay dedup does not silently suppress a
detection and make coverage look worse than it is; the dedup cases deliberately
share one.

    export STRAIKER_CODING_KEY=...
    python3 spec/portkey_coding_suite.py [--limit N] [--category A_benign]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from portkey_coding_cases import build_cases  # noqa: E402

DETECT_URL = os.getenv("STRAIKER_DETECT_URL", "https://api.prod.straiker.ai/api/v1/detect")
X_TOOL = os.getenv("STRAIKER_X_TOOL", "portkey-claude-code")
CONCURRENCY = int(os.getenv("SUITE_CONCURRENCY", "5"))
TIMEOUT = float(os.getenv("SUITE_TIMEOUT", "60"))


def _headers(key: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {key}",
            "x-tool": X_TOOL, "Straiker-Debug": "TRUE"}


def _retarget_session(envelope: dict, session: str) -> dict:
    """Give a case its own session so replay dedup does not mask its result."""
    body = (envelope.get("request") or {}).get("json")
    if isinstance(body, dict) and isinstance(body.get("metadata"), dict):
        uid = body["metadata"].get("user_id")
        if isinstance(uid, str) and uid.startswith("{"):
            try:
                parsed = json.loads(uid)
                parsed["session_id"] = session
                body["metadata"]["user_id"] = json.dumps(parsed)
            except json.JSONDecodeError:
                pass
    return envelope


def _straiker_block(payload: dict) -> dict:
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("straiker"), dict):
        return data["straiker"]
    return payload.get("straiker") or {}


def run_case(client: httpx.Client, key: str, case) -> dict:
    started = time.perf_counter()
    record = {"category": case.category, "name": case.name, "expect_detection": case.expect_detection,
              "expect_event": case.expect_event, "notes": case.notes, "tags": list(case.tags)}
    try:
        r = client.post(DETECT_URL, json=case.envelope, headers=_headers(key), timeout=TIMEOUT)
        record["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        record["http_status"] = r.status_code
        try:
            payload = r.json()
        except ValueError:
            record["error"] = r.text[:300]
            return record
        st = _straiker_block(payload)
        events = st.get("events") or []
        record.update({
            "verdict": payload.get("verdict"),
            "phase": st.get("phase"), "call_kind": st.get("call_kind"),
            "wire_format": st.get("wire_format"),
            "events_scored": st.get("events_scored"), "events_replayed": st.get("events_replayed"),
            "events_dropped_over_cap": st.get("events_dropped_over_cap"),
            "event_names": [e.get("hook_event_name") for e in events],
            "max_score": max([e.get("score") or 0.0 for e in events], default=0.0),
            "categories": sorted({e.get("score_category") for e in events if e.get("score_category")}),
            "severities": sorted({e.get("severity") for e in events if e.get("severity")}),
            "actions": sorted({e.get("action") for e in events if e.get("action")}),
            "reasons": [e.get("reason") for e in events if e.get("reason")][:3],
        })
        record["detected"] = record["max_score"] > 0.0
        record["blocked"] = payload.get("verdict") is False or "block" in (record["actions"] or [])
        record["event_seen"] = (case.expect_event in (record["event_names"] or [])) if case.expect_event else None
        record["pass"] = record["detected"] == case.expect_detection
    except Exception as exc:  # noqa: BLE001
        record["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        record["error"] = f"{type(exc).__name__}: {exc}"[:300]
        record["pass"] = False
    return record


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--category", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    key = os.getenv("STRAIKER_CODING_KEY") or os.getenv("STRAIKER_API_KEY")
    if not key:
        print("STRAIKER_CODING_KEY (or STRAIKER_API_KEY) is not set", file=sys.stderr)
        return 1

    base_session = f"pksuite-{uuid.uuid4().hex[:8]}"
    cases = build_cases(base_session)
    if args.category:
        cases = [c for c in cases if c.category.startswith(args.category)]
    if args.limit:
        cases = cases[: args.limit]

    for c in cases:
        if "dedup" not in c.tags and "replay" not in c.tags:
            _retarget_session(c.envelope, f"{base_session}-{uuid.uuid4().hex[:6]}")

    print(f"suite: {len(cases)} cases -> {DETECT_URL}  (x-tool: {X_TOOL})")
    print(f"concurrency {CONCURRENCY}, base session {base_session}\n")

    started = time.time()
    with httpx.Client(timeout=TIMEOUT) as client:
        # dedup cases must run in order on one session; everything else is order-independent
        serial = [c for c in cases if "dedup" in c.tags or "replay" in c.tags]
        parallel = [c for c in cases if c not in serial]
        results: list[dict] = []
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            results.extend(pool.map(lambda c: run_case(client, key, c), parallel))
        for c in serial:
            results.append(run_case(client, key, c))
    elapsed = time.time() - started

    order = {c.name: i for i, c in enumerate(cases)}
    results.sort(key=lambda r: (r["category"], order.get(r["name"], 0)))

    out = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results",
                                   f"portkey_coding_suite_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump({"detect_url": DETECT_URL, "x_tool": X_TOOL, "base_session": base_session,
                   "started_utc": datetime.now(timezone.utc).isoformat(), "elapsed_s": round(elapsed, 1),
                   "results": results}, fh, indent=2)

    summarize(results, elapsed, out)
    return 0


def summarize(results: list[dict], elapsed: float, out: str) -> None:
    cats: dict[str, list[dict]] = {}
    for r in results:
        cats.setdefault(r["category"], []).append(r)

    print(f"{'category':<18} {'n':>3} {'detected':>9} {'expected':>9} {'pass':>6}  notes")
    print("-" * 78)
    for cat in sorted(cats):
        rows = cats[cat]
        det = sum(1 for r in rows if r.get("detected"))
        exp = sum(1 for r in rows if r.get("expect_detection"))
        ok = sum(1 for r in rows if r.get("pass"))
        inverted = " (inverted: detections are FPs)" if exp == 0 else ""
        print(f"{cat:<18} {len(rows):>3} {det:>9} {exp:>9} {ok:>4}/{len(rows):<2}{inverted}")

    errs = [r for r in results if r.get("error")]
    http_bad = [r for r in results if r.get("http_status") not in (200, None)]
    lat = sorted(r["latency_ms"] for r in results if r.get("latency_ms"))
    fp = [r for r in results if not r.get("expect_detection") and r.get("detected")]
    fn = [r for r in results if r.get("expect_detection") and not r.get("detected")]

    print("-" * 78)
    print(f"total {len(results)} | pass {sum(1 for r in results if r.get('pass'))} | "
          f"errors {len(errs)} | non-200 {len(http_bad)} | {elapsed:.0f}s")
    if lat:
        print(f"latency ms: p50 {lat[len(lat)//2]:.0f}  p95 {lat[int(len(lat)*0.95)-1]:.0f}  max {lat[-1]:.0f}")
    print(f"false positives: {len(fp)}   missed detections: {len(fn)}")
    if fp:
        print("\nFALSE POSITIVES (benign/chatter that scored):")
        for r in fp[:15]:
            print(f"  {r['category']}/{r['name']}: score={r.get('max_score')} cats={r.get('categories')}")
    if fn:
        print("\nMISSED (expected a detection, got none):")
        for r in fn[:40]:
            print(f"  {r['category']}/{r['name']}: events={r.get('event_names')} scored={r.get('events_scored')}")
    if errs:
        print("\nERRORS:")
        for r in errs[:10]:
            print(f"  {r['category']}/{r['name']}: {r['error']}")
    print(f"\nraw -> {out}")


if __name__ == "__main__":
    raise SystemExit(main())
