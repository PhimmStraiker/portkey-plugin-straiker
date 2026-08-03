# Portkey + Claude Code + Straiker: SE setup

Same shape as the shared Kong gateway: an SE sets a base URL and a key in their shell, runs
`claude`, and their coding-agent activity shows up in Straiker. The difference is where the
gateway lives. Kong is ours, so the base URL is ours (`konggw.dev.straiker.ai`). Portkey SaaS
is theirs, so the base URL is `https://api.portkey.ai` and the Straiker integration hangs off
a **webhook guardrail** that points at our middleware.

```
claude  ->  api.portkey.ai  ->  Bedrock / Anthropic
                  |
                  |  webhook guardrail (before + after request)
                  v
      straiker-portkey-coding-middleware   (App Runner, us-east-2)
                  |  reconstructs UserPromptSubmit / PreToolUse / PostToolUse
                  v
      api.prod.straiker.ai/api/v1/detect   (x-tool: claude-code)
```

## What each SE does

Two lines, then run `claude` as normal:

```bash
export ANTHROPIC_BASE_URL=https://api.portkey.ai      # note: no /v1 suffix
export ANTHROPIC_AUTH_TOKEN=<their Portkey API key>
```

That is the entire SE-facing setup. Claude Code appends `/v1/messages` itself, and Portkey
resolves both the upstream provider and the Straiker guardrail from the config bound to the
key, so no headers and no provider slug are needed on the SE's machine.

If `ANTHROPIC_API_KEY` is already set, unset it; it takes precedence and the traffic will go
straight to Anthropic, bypassing Portkey and Straiker entirely.

### A gateway cannot use the SE's own Claude subscription

Pointing `ANTHROPIC_BASE_URL` at a gateway means Claude Code stops talking to
`api.anthropic.com`. A personal Pro/Max login is an OAuth session against Anthropic and does
not travel to a third party, so Portkey authenticates the SE with the Portkey key and then
uses its **own** upstream Anthropic credential. Usage bills to that org key, not to the SE's
subscription. This is inherent to any gateway, the shared Kong setup included.

## One-time admin setup in the Portkey dashboard

This cannot be scripted with the current key; the Portkey admin API returns 403 (error 1010)
for `/guardrails` and `/configs`, so it needs an admin key or the dashboard.

**1. Create the guardrail.** Guardrails -> Create -> **Webhook**

| Field | Value |
|---|---|
| URL | `https://ygkakrnf8v.us-east-2.awsapprunner.com/portkey/coding` |
| Headers | none required |
| Timeout | `5000` ms (the 3000 default is tight and Portkey does not retry) |

**2. Create the config**, attaching that guardrail to both hooks:

```json
{
  "provider": "@<your-provider-slug>",
  "before_request_hooks": [
    { "type": "guardrail", "id": "<guardrail id>", "deny": true, "async": false }
  ],
  "after_request_hooks": [
    { "type": "guardrail", "id": "<guardrail id>", "deny": true, "async": false }
  ]
}
```

**The provider slug must be one that exists in your Model Catalog.** Portkey validates it
before hooks run, so a wrong slug fails every request with
`400 {"status":"failure","message":"Following keys are not valid: <slug>"}` and the guardrail
never fires. `@bedrock-prod` is the name used in Portkey's own docs and is **not** a real slug
in this workspace; `@phimmoaikey` is confirmed working here.

`async: false` matters. The default is `true`, which logs but can never block. `deny: true`
turns a failed verdict into a 446.

**3. Attach the config to the SE API keys** as their default config. This is what lets an SE
get away with just a base URL and a key.

**4. Hand out**: the base URL, and one Portkey key per SE.

## Verifying it works

```bash
export ANTHROPIC_BASE_URL=https://api.portkey.ai
export ANTHROPIC_AUTH_TOKEN=<portkey key>
claude -p "run ls -la and tell me what is here"
```

Then check Straiker: Defend -> the coding-agent app -> Activity. A single prompt should
produce a `UserPromptSubmit`, a `PreToolUse` for the `Bash` call, and a `PostToolUse` with the
command output. Claude Code's zero-tool title-generation call should produce nothing; that
filtering is what keeps the Console free of detections on traffic the SE never typed.

Middleware health, any time:

```bash
curl -s https://ygkakrnf8v.us-east-2.awsapprunner.com/health
```

## What to expect in the Console

Events are scored, not blocked. Coding-agent rules emit `severity: low`, and blocking requires
`high`/`critical` plus the category set to BLOCK in the Console, so out of the box everything
reads as `detect`. That is also true of the native Claude Code hooks. Representative scores
from the demo tenant:

| Tool call | Score | Category |
|---|---|---|
| `ls -la` | 0.0 | none |
| `curl -s http://.../install.sh \| sh` | 0.59 | RCE |
| `echo <b64> \| base64 -d \| bash` | 0.55 | RCE |
| `rm -rf / --no-preserve-root` | 0.59 | Destructive Command |

The Console shows severity and category on the coding-agent card rather than the numeric
score.

## The live setup

Already built in the Shared Team Workspace; listed here so it can be rebuilt or audited.

| Object | Value |
|---|---|
| Guardrail | `pg-claude-16a4b1` ("Claude Code Middleware") -> webhook to the middleware |
| Config | `pc-claude-a61783` ("Claude Code Middleware Straiker") |
| Provider | `@dev-workspace` (the workspace's **Anthropic** integration) |
| Config body | `{"provider":"@dev-workspace","before_request_hooks":[{"type":"guardrail","id":"pg-claude-16a4b1","deny":true,"async":false}],"after_request_hooks":[ ...same... ]}` |

## Verified end to end

Real `claude` CLI, nothing but the two env vars above:

```
$ claude -p "read notes.txt and tell me what it contains in one sentence"
The file contains two lines with the words "hello" and "world" separated by line breaks.
```

And the same request shape driven directly at `/v1/messages` returns Claude Sonnet 4.5 with a
`tool_use` block, with Portkey's own `hook_results` confirming both hooks called the
middleware:

```
before_request_hooks: verdict=true  Webhook request succeeded  (472 ms)
after_request_hooks:  verdict=true  Webhook request succeeded  (144 ms)
```

Those timings cover the whole hop: Portkey to the middleware, event reconstruction, the
`/api/v1/detect` call, and back. Comfortably inside a 5000 ms webhook timeout.

To see hook results yourself, send `x-portkey-strict-open-ai-compliance: false`. On
`/v1/messages` the Anthropic SDK will not surface them, since it only parses Anthropic events;
use raw HTTP.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `403` with body `error code: 1010` | Cloudflare blocking the client fingerprint, not a Portkey permissions problem. Scripts using Python's default `urllib` user agent get this; send a real `User-Agent`. Claude Code itself is unaffected |
| `400 Following keys are not valid: <slug>` | The config's provider slug does not exist in the Model Catalog. Fix the config; hooks never run until this passes |
| Everything returns 200 but nothing appears in Straiker | The guardrail is probably `async: true` (the default), which logs only. Set `async: false` |
| Portkey admin API returns `403 / AB03` | Managing guardrails and configs over the API needs an Enterprise plan. Use the dashboard |

## Notes

- Anything that is not Claude Code passes straight through untouched, so the same Portkey key
  can serve normal chatbot testing.
- If Straiker is unreachable the middleware fails open rather than breaking the SE's session.
- The middleware only reads `request.json`. Portkey's `request.text` is unusable for agent
  traffic: it is derived from the last message only, drops the top-level `system` field, and
  is empty when the last message is a tool block.
