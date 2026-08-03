# Straiker DefendAI for the Portkey AI Gateway

Two things live in this repo:

| | What it is | Use it when |
|---|---|---|
| **`middleware/`** | A small service that sits behind a Portkey **Webhook guardrail** and scores **Claude Code** traffic through Straiker's coding-agent pipeline | Portkey **SaaS**, or anything with Claude Code in it. Needs no Portkey plugin and no argus change |
| **`plugins/straiker/`** | A custom TypeScript plugin compiled into the gateway | You run the Portkey gateway **yourself** and want chatbot / agentic scoring in-process |

**Start with the middleware.** Portkey SaaS compiles plugins into its binary, so custom plugin
code cannot be installed there; the Webhook guardrail is the only customer-code extension
point, and it is available on all plans. See **[`deploy/DEPLOYMENT.md`](deploy/DEPLOYMENT.md)**
for the full record and **[`deploy/SE_SETUP.md`](deploy/SE_SETUP.md)** for the two env vars an
SE needs.

```
claude -> api.portkey.ai -> Anthropic
              |  webhook guardrail (before + after)
              v
      middleware  ->  /api/v1/detect  (x-tool: claude-code)
```

Verified end to end with the real `claude` CLI: both hooks call the middleware, and 19 real
captured Claude Code sessions replay at 100% recall against the native hook events.

---

## The TypeScript plugin (self-hosted gateways)

Custom Portkey plugin that scores requests and responses against the Straiker DefendAI detect API. Mirrors the Kong, Azure APIM and LiteLLM integrations: same payload, same agent-loop dedup, same blocking semantics. Single-turn chatbot apps hit `/api/v1/detect`; multi-turn / tool-calling agents hit `/api/v1/detect?agentic` with the full `messages[]` (including `tool_calls` and tool results).

## Architecture

```
Client ──▶ Portkey AI Gateway ──▶ provider (OpenAI, Azure OpenAI, Anthropic, …)
              │  beforeRequestHook                     │
              │      └──▶ Straiker /detect[?agentic]   │
              │  afterRequestHook                       │
              │      └──▶ Straiker /detect[?agentic]   │
              │  guardrail verdict                      │
              ▼
         HTTP 246 if deny=true and verdict=false
```

The plugin is a single TypeScript handler that answers both `beforeRequestHook` and `afterRequestHook`. It calls Straiker's detect endpoint, reads back a score and a turn id, and returns `verdict: false` when the score crosses the threshold. Whether that becomes a hard block or just an observability event is up to the customer's Portkey config — `deny: true` enforces, `deny: false` is observe-only.

## What's in the box

```
portkey-plugin-straiker/
├── plugins/straiker/
│   ├── manifest.json              # plugin metadata, credentials schema, function defs
│   ├── detect.ts                  # main handler — both hooks, both modes
│   ├── helpers.ts                 # tool_call reshape, agentic message builder, dedup helpers
│   └── straiker.test.ts           # jest unit tests
├── examples/
│   ├── conf.json                  # gateway-side credentials block
│   ├── client-config-chatbot.json
│   └── client-config-agentic.json
├── INSTALL.md                     # how to drop into a Portkey fork
├── package.json
└── tsconfig.json
```

## How it differs from the rest of the family

| | Kong (Lua) | Azure APIM (XML) | LiteLLM (Python) | Portkey (this plugin) |
|---|---|---|---|---|
| Block status | HTTP 403 | HTTP 403 | HTTP 403 (raises HTTPException) | HTTP 246 (Portkey custom) when `deny: true` |
| Block decision | plugin emits | policy emits | guardrail emits | plugin returns `verdict: false`, customer config sets `deny: true` |
| Agentic toggle | `agentic: true` | `straikerAgentic = true` | `agentic: true` | `parameters.agentic = true` |
| Endpoint | `/detect` or `/detect?agentic` | same | same | same |
| Payload shape | identical | identical | identical | identical |
| Tool-call reshape | function.arguments → input object | same | same | same (port of Kong's logic) |
| Agent-loop dedup | last role tool/assistant → skip pre; tool_calls → skip post | same | same | same |
| Streaming | skip post-call | skip post-call | skip post-call | skip post-call (`response.json` null) |

The contract on the Straiker side is identical across all four. Run the same agentic test through Kong, APIM, LiteLLM and Portkey and the Console verdicts match.

## Configuration

All knobs are passed as `parameters` on the guardrail entry in the customer's Portkey config. Credentials live in `conf.json` on the gateway side.

| Parameter | Default | Description |
|---|---|---|
| `agentic` | `false` | When `true`, calls `/detect?agentic` and forwards the full conversation including `tool_calls` and tool results. |
| `threshold` | `0.5` | Score above this returns `verdict: false`. |
| `source` | `portkey-plugin` (`portkey-plugin-agentic` when `agentic: true`) | Straiker Console app name. Should match an existing application; in agentic mode this distinguishes agentic apps from chatbot apps. |
| `destination` | `api.openai.com` | Upstream model host (Console attribution only). |
| `timeoutMs` | `5000` | Per-call HTTP timeout for `/detect`. |
| `failOpen` | `false` | When `true`, returns `verdict: true` on Straiker errors. |

| Credential | Required | Description |
|---|---|---|
| `apiKey` | yes | Straiker DefendAI app key. |
| `detectUrl` | no (defaults to `https://api.prod.straiker.ai/api/v1/detect`) | Override for self-hosted Straiker or staging. |

## Recommended controls for agentic apps

The standard threat model for agentic applications is **Indirect Prompt Injection** — adversarial content reaching the agent through tool results, RAG documents, or external data rather than the user prompt. This is built into Straiker's **Agentic Guardrails** category and is the right primary control for agentic apps.

`LLM Evasion` is shaped for chatbot single-turn inputs and over-fires on benign agentic content like `"Find me information on the topic Acme Project."`. Leaving it in Block mode for an agentic app produces frequent pre-call false positives.

| Category | Chatbot | Agentic | Notes |
|---|---|---|---|
| LLM Evasion | Block | **Detect** (off) | Over-fires on benign agentic prompts; rely on Agentic Guardrails instead. |
| Agentic Guardrails (Indirect Injection) | N/A | **Block** | Primary control for agentic; catches injection in tool results and RAG content. |
| Email / SSN / Credit Card (regex) | Block | **Block** | Deterministic; ideal for output gating (e.g., PII leaking through a tool result into the final answer). |
| Tool Misuse | N/A | Block | Agentic-specific. |
| Resource Exhaustion | N/A | Block | Agentic-specific. |

## Install

See [INSTALL.md](INSTALL.md). Drop `plugins/straiker/` into a Portkey gateway checkout, add credentials to `conf.json`, run `npm run build-plugins`, restart.

## Test

```bash
npm install
npm test
```

The unit tests stub Portkey's `post` helper so they run offline. Coverage:

- benign pre-call → `verdict: true`, score in `data`
- score above threshold → `verdict: false`, `turn_id` and `score` surfaced
- `agentic: true` routes to `/detect?agentic` and emits the agentic payload shape
- pre-call dedup on continuations (last role `tool`/`assistant`)
- post-call dedup on intermediate iterations (response carries `tool_calls`)
- tool-call reshape: OpenAI `function.arguments` JSON string → Straiker `input` object
- fail-open / fail-closed paths when Straiker is unreachable
- streaming responses skip post-call

End-to-end testing requires a live Portkey gateway with the plugin built in; see INSTALL.md.

## Related

- [`apim-policy-straiker`](https://github.com/PhimmStraiker/apim-policy-straiker) — Azure APIM policy fragments.
- [`kong-plugin-straiker`](https://github.com/PhimmStraiker/kong-plugin-straiker) — Kong Lua plugin.
- [`litellm-straiker-agentic`](../litellm-straiker-agentic/) — LiteLLM `CustomGuardrail` with the same dedup and reshape rules.

## Versioning

| Version | Date | Notes |
|---|---|---|
| 0.1.0 | 2026-05 | Initial scaffold. Single handler, both hooks, chatbot + agentic, full dedup + reshape, jest unit tests. |
