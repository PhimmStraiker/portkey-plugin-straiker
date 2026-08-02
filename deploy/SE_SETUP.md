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
export ANTHROPIC_AUTH_TOKEN=<your Portkey API key>
```

That is the whole SE-facing setup, provided the admin step below attached the config to the
key. Claude Code appends `/v1/messages` itself, and Portkey resolves the provider and the
Straiker guardrail from the config bound to that key.

If a key has no default config attached, the SE also needs:

```bash
export ANTHROPIC_CUSTOM_HEADERS="x-portkey-api-key: <your Portkey API key>
x-portkey-provider: @bedrock-prod"
```

Prefer binding the config to the key so SEs never have to set headers.

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
  "provider": "@bedrock-prod",
  "before_request_hooks": [
    { "type": "guardrail", "id": "<guardrail id>", "deny": true, "async": false }
  ],
  "after_request_hooks": [
    { "type": "guardrail", "id": "<guardrail id>", "deny": true, "async": false }
  ]
}
```

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

## Notes

- Anything that is not Claude Code passes straight through untouched, so the same Portkey key
  can serve normal chatbot testing.
- If Straiker is unreachable the middleware fails open rather than breaking the SE's session.
- The middleware only reads `request.json`. Portkey's `request.text` is unusable for agent
  traffic: it is derived from the last message only, drops the top-level `system` field, and
  is empty when the last message is a tool block.
