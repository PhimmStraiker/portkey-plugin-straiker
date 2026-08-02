# Straiker coding-agent middleware for Portkey

Scores Claude Code traffic flowing through Portkey using Straiker's existing coding-agent
pipeline, with no Portkey plugin and no argus change.

## Why this exists

Portkey SaaS cannot run our code; plugins are compiled into their gateway binary. The one
customer-code extension point on SaaS is the **Webhook guardrail**, which is just a URL
Portkey POSTs to. This service takes that URL.

The normal chatbot guardrail path does not work for Claude Code. argus builds its prompt
from `request.text`, and Portkey derives that field from the **last message only**,
**excludes the top-level `system` field**, and leaves it empty when the last message is a
`tool_use` / `tool_result` block (those have no `.text`). In a Claude Code tool loop that is
not the developer's intent, so the guardrail both false-positives on scaffolding and misses
the real tool calls.

This service reads `request.json` instead, which Portkey forwards complete and unmodified,
and reconstructs the hook events an endpoint-installed Straiker hook would have emitted.

```
Claude Code -> Portkey SaaS -> webhook -> THIS SERVICE
                                            | reconstructs hook events
                                            v
                                 POST /api/v1/detect  (x-tool: claude-code)
                                 existing public API, unchanged
```

## What it reconstructs

One developer action fans into 4-30 model calls, and the hook events are spread across them.

| Hook event | Recovered from | Blocks? |
|---|---|---|
| `UserPromptSubmit` | last user text block, scaffolding stripped | yes, before the model is called |
| `PreToolUse` | assistant `tool_use` block in the response | only when non-streaming (see limits) |
| `PostToolUse` | `tool_result` in the next request, matched by `tool_use_id` | yes, before the result reaches the model |
| `Stop` | `stop_reason: end_turn` with final text | telemetry only |

Three rules make it correct:

- **Chatter filter.** Zero-tool calls are Claude Code's own machinery (title generation,
  suggestion mode, recaps), roughly a quarter of all traffic. Scoring them is what produces
  detections on text the developer never wrote.
- **Dedup.** The transcript is resent on every call, so events are claimed once per session
  (`pre:<id>` / `post:<id>` / `prompt:<hash>` / `stop:<hash>`).
- **Session identity.** `metadata.user_id` is a JSON *string* holding a stable `session_id`.
  Without one the service emits nothing rather than fragment the backend's event trace.

## Verified parity

`python3 spec/parity_check.py` replays 19 real captured Claude Code sessions and diffs the
reconstructed events against the native hook events recorded from the same runs:

```
event                   recall   precision   (mine / native / matched)
UserPromptSubmit        100.0%       95.0%   (20 / 19 / 19)
PreToolUse              100.0%      100.0%   (33 / 33 / 33)
PostToolUse             100.0%       78.8%   (33 / 26 / 26)
```

Recall is the gate; every native event is reconstructed. The extra events are the gateway
being more complete, not wrong: the surplus `PostToolUse` are error results from blocked or
failed attack scenarios that the native hook skips but the wire still carries, and the extra
`UserPromptSubmit` is a subagent turn that issues its own prompt.

## Run it

```bash
pip install -r requirements.txt
export STRAIKER_API_KEY=<coding-agent app key>
uvicorn app:app --host 0.0.0.0 --port 8080
```

Point the Portkey Webhook guardrail at `https://<host>/portkey/coding`.

### Configuration

| Variable | Default | Notes |
|---|---|---|
| `STRAIKER_DETECT_URL` | `https://api.prod.straiker.ai/api/v1/detect` | The one setting that changes between deployment modes. Rejects a `/webhook` path, which does not run the coding pipeline |
| `STRAIKER_API_KEY` | — | Use a **separate key** for the coding-agent application; the backend files an app under Coding Agents or Custom Agents based on the traffic it receives, and one key serving both mixes them |
| `STRAIKER_BLOCK_ENABLED` | `true` | `false` makes the service observe-only |
| `STRAIKER_CHATTER_FILTER` | `true` | Leave on; this is the false-positive fix |
| `STRAIKER_DETECT_TIMEOUT` | `2.5` | Must stay well under Portkey's webhook timeout so this service fails open deliberately rather than letting Portkey time out |
| `STRAIKER_DEDUP_TTL` | `3600` | Seconds an emitted event is remembered |
| `STRAIKER_DEFAULT_USER_NAME` | `portkey-coding` | Fallback identity; must be stable per session |
| `STRAIKER_SIGN_PAYLOADS` | `true` | Sends `X-Straiker-Webhook-Signature` / `-Timestamp` |

### Portkey guardrail settings that matter

- `async: false` — the default `true` is **logging only and cannot block**
- `deny: true` — turns a false verdict into a 446 and kills the request
- `failOnError: true` — fail-open is Portkey's default at every layer
- raise the timeout from the 3s default; Portkey performs **no retries**
- give Claude Code its **own Portkey config/key** so ordinary chatbot traffic keeps going
  straight to argus and gains no added latency

## Deployment modes

The service is written so only `detect_client` knows about the network; everything else,
including all event reconstruction, is pure. That makes the path to in-tree a transport swap.

| Mode | `STRAIKER_DETECT_URL` | Extra hop |
|---|---|---|
| Standalone demo | public `api.prod.straiker.ai` | one public round trip |
| Bolt-on in front of argus | cluster-local argus service | ~1 ms |
| Merged into argus | n/a; call `HookDispatcher.run` in-process | none |

## Limits, stated plainly

- **`PreToolUse` cannot block on streaming.** Portkey evaluates output guardrails on the
  assembled response after the stream completes and takes no action on the result. Claude
  Code streams by default, so on SaaS those events are scored off the critical path and
  surfaced, not enforced. `UserPromptSubmit` and `PostToolUse` do block. True pre-execution
  tool blocking needs a self-hosted Portkey gateway plugin.
- **Nothing blocks until Console policy is tuned.** Coding-agent rules return `severity: low`
  and every category defaults to DETECT. This is equally true of the native hooks.
- **Hygiene checks are not reachable from a gateway.** `config.settings`, `cwd` and
  `transcript_path` come from the developer's filesystem, which no gateway can see.
- **Portkey bypass surfaces to be aware of:** `/v1/proxy/*` skips all guardrails, and Nitro
  Mode silently disables input guardrails.
- Portkey strips request headers from webhook payloads, so Claude Code is identified from
  the body (core tool names and system markers) rather than the `claude-cli/` user agent.
