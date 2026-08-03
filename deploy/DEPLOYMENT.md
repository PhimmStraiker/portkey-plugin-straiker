# Portkey + Claude Code + Straiker: consolidated record

Everything built, deployed and verified for the SE demo environment. Demo tenant only;
nothing here touches a customer environment.

Companion docs: `deploy/SE_SETUP.md` (the SE-facing extract) and `middleware/README.md`
(how the service works internally).

---

## 1. The problem

Claude Code traffic through Portkey broke on the normal chatbot guardrail path. argus builds
its prompt from Portkey's `request.text`, and Portkey derives that field from the **last
message only**, **excludes the top-level `system` field**, and leaves it empty when the last
message is a `tool_use` / `tool_result` block, which is the common case in an agent loop. So
Straiker was scoring scaffolding rather than developer intent: false positives on Claude
Code's own machinery, and no visibility into the tool calls that actually matter.

A second problem sits underneath it. One developer action fans into 4-30 model calls, the
median request body is ~137 KB (~35k tokens), and roughly a quarter of all calls are zero-tool
utility traffic (title generation, suggestion mode, recaps). Scoring that blob wholesale is
both wrong and slow.

## 2. The approach

Portkey SaaS cannot run our code; plugins are compiled into their gateway binary. The one
customer-code extension point is the **Webhook guardrail**, which is just a URL Portkey posts
to. So we host that URL ourselves.

```
claude  ->  api.portkey.ai  ->  Anthropic (@dev-workspace)
                  |
                  |  webhook guardrail, before + after request
                  v
      straiker-portkey-coding-middleware      (App Runner, us-east-2)
                  |  reads request.json (never request.text)
                  |  reconstructs UserPromptSubmit / PreToolUse / PostToolUse / Stop
                  v
      api.prod.straiker.ai/api/v1/detect      (x-tool: claude-code)
```

The middleware reconstructs the hook events an endpoint-installed Straiker hook would have
emitted, then posts them to the **existing** coding-agent API. No Portkey plugin, no argus
change. Event synthesis is a pure, I/O-free module, so it lifts into argus unchanged if this
ever folds in-tree; only `detect_client` touches the network.

Three rules make it correct: a **chatter filter** that drops zero-tool utility calls, **dedup**
so a resent transcript is scored once, and **session identity** parsed from `metadata.user_id`
(a JSON string holding a stable `session_id`).

## 3. What is deployed

| | |
|---|---|
| Service | `straiker-portkey-coding-middleware` (AWS App Runner) |
| Region | **us-east-2**, account `108782090038` |
| Health | https://ygkakrnf8v.us-east-2.awsapprunner.com/health |
| **Webhook URL** | **https://ygkakrnf8v.us-east-2.awsapprunner.com/portkey/coding** |
| Service ARN | `arn:aws:apprunner:us-east-2:108782090038:service/straiker-portkey-coding-middleware/83ea02c7939d4b6c8f481c39c4de609c` |
| Image | `108782090038.dkr.ecr.us-east-2.amazonaws.com/straiker-portkey-coding-middleware` |
| Instance | 1 vCPU / 2 GB, autodeploy off |
| Detect target | `https://api.prod.straiker.ai/api/v1/detect` |

Redeploy:

```bash
set -a && source "$HOME/Projects/Straiker Projects/.env" && set +a   # AWS SSO creds
set -a && source deploy/secrets.env.local && set +a                  # Straiker key
./deploy/deploy.sh
```

Idempotent: rebuilds and pushes the image, creates IAM roles only if missing, creates or
updates the service, waits for `RUNNING`.

## 4. Portkey objects (Shared Team Workspace)

| Object | Value |
|---|---|
| Guardrail | `pg-claude-16a4b1` - "Claude Code Middleware", webhook -> the URL above |
| Config | `pc-claude-a61783` - "Claude Code Middleware Straiker" |
| Provider | `@dev-workspace` - the workspace's existing **Anthropic** integration |
| SE key | Service-type key with `pc-claude-a61783` bound as its default config |

Config body:

```json
{
  "provider": "@dev-workspace",
  "before_request_hooks": [
    { "type": "guardrail", "id": "pg-claude-16a4b1", "deny": true, "async": false }
  ],
  "after_request_hooks": [
    { "type": "guardrail", "id": "pg-claude-16a4b1", "deny": true, "async": false }
  ]
}
```

`async: false` is required; the default `true` logs but can never block.

## 5. What an SE does

```bash
export ANTHROPIC_BASE_URL=https://api.portkey.ai      # no /v1 suffix
export ANTHROPIC_AUTH_TOKEN=<their Portkey key>
```

Then `claude` as normal. Unset `ANTHROPIC_API_KEY` if present; it takes precedence and the
traffic bypasses Portkey and Straiker entirely.

**A gateway cannot ride the SE's own Claude subscription.** Pointing `ANTHROPIC_BASE_URL` at a
gateway stops Claude Code talking to `api.anthropic.com`, and a personal Pro/Max OAuth session
does not travel to a third party. Portkey authenticates the SE with the Portkey key and uses
its own upstream Anthropic credential, so usage bills to `@dev-workspace`, not to the SE. Same
as the shared Kong gateway.

## 6. Credentials

No secret is committed. Verified with `git grep` across tracked files and a scan of every
staged diff before each commit.

| Secret | Where |
|---|---|
| Straiker coding-agent app key | App Runner runtime env var; local copy in `deploy/secrets.env.local` |
| Portkey SE key + config id | `deploy/secrets.env.local` |
| AWS credentials | `Straiker Projects/.env` (SSO, expire regularly) |
| Anthropic key | Not needed by us; it already lives inside the Portkey `@dev-workspace` integration |

`deploy/secrets.env.local` is gitignored via `deploy/*.local`.

The key is passed as a runtime env var (App Runner encrypts these at rest). Secrets Manager is
available with `USE_SECRETS_MANAGER=1` but is off by default; for a demo-tenant key it added
IAM and failure modes without buying much.

## 7. Verification

**Real CLI, nothing but the two env vars:**

```
$ claude -p "read notes.txt and tell me what it contains in one sentence"
The file contains two lines with the words "hello" and "world" separated by line breaks.
```

**Both hooks calling the middleware**, from Portkey's own `hook_results`:

```
before_request_hooks: verdict=true  Webhook request succeeded  (472 ms)
after_request_hooks:  verdict=true  Webhook request succeeded  (144 ms)
```

Returned `claude-sonnet-4-5-20250929`, `stop_reason: tool_use`, `Bash {"command":"ls -la"}`.

**Offline parity** (`python3 spec/parity_check.py`) replays 19 real captured Claude Code
sessions and diffs the reconstructed events against the native hook events from the same runs:

```
event                   recall   precision   (mine / native / matched)
UserPromptSubmit        100.0%       95.0%   (20 / 19 / 19)
PreToolUse              100.0%      100.0%   (33 / 33 / 33)
PostToolUse             100.0%       78.8%   (33 / 26 / 26)
```

Recall is the gate. The surplus events are the gateway seeing more than the endpoint hook:
error `tool_result`s from blocked attack scenarios the native hook skips, and a subagent turn
issuing its own prompt.

**Detection scores** on the demo tenant, posted with `Straiker-Debug: TRUE`:

| Tool call | Score | Severity | Action | Category |
|---|---|---|---|---|
| `ls -la` | 0.0 | none | detect | - (no false positive) |
| `curl -s http://.../install.sh \| sh` | 0.59 | low | detect | RCE, Reverse Shell / Tunnel Establishment |
| `echo <b64> \| base64 -d \| bash` | 0.55 | low | detect | RCE, Base64 Decode Piped to Shell Execution |
| `rm -rf / --no-preserve-root` | 0.59 | low | detect | Destructive Command |
| `cat ~/.aws/credentials` | 0.2 | low | detect | File Access Boundaries, downgraded by adaptive gate |

## 8. Scores, and why nothing blocks

Scores are integers 0-100 divided by 100 at the boundary
(`rule_based_engine/__init__.py`): sub-30 is dropped to 0, `>=70` is `high`, `>=30` is `low`.

The ceiling matters. `matcher.py` caps a rule by its authored severity:

```python
_SEVERITY_CAP = {"low": 59, "high": 79}
```

A rule tagged `severity: low` can never exceed **59**, but the band needs **70** for `high`,
and `action: block` requires `high`/`critical` plus the category set to BLOCK in the Console.
So a low-tagged rule cannot block by arithmetic, not by policy. That is why `curl | sh` and
`rm -rf /` both land on exactly 0.59. The 0.2 is a different path: the adaptive gate sets it
directly. This is equally true of the native Claude Code hooks.

The Console does not render the numeric score for coding-agent turns; `coding-agent.svelte`
shows severity and category, and `session-timeline.svelte` gates its block icon on
`turn.score === 1`, a strict equality that a 0.59 never satisfies.

## 9. Gotchas worth keeping

| Symptom | Cause |
|---|---|
| `403` with body `error code: 1010` | **Cloudflare** blocking the client fingerprint, not Portkey permissions. Python's default `urllib` user agent triggers it; send a real `User-Agent`. Cost hours of misdiagnosis, including a wrong "you need Enterprise" conclusion |
| `400 Following keys are not valid: <slug>` | The config's provider slug does not exist in the Model Catalog. Portkey validates it **before** hooks run, so the guardrail never fires. `@bedrock-prod` is Portkey's doc example, not a real slug here |
| Everything 200 but nothing in Straiker | Guardrail left at `async: true`, which logs only |
| Config contains `"<guardrail id>"` | A template placeholder pasted verbatim. Use real ids |
| Claude Code ignores the gateway | `ANTHROPIC_API_KEY` is set and takes precedence; Claude Code warns about this |

Portkey also strips request headers from webhook payloads, so Claude Code is identified from
the body (core tool names plus `cc_version=` / `cc_entrypoint=` system markers) rather than the
`claude-cli/` user agent.

## 10. Known limits

- **`PreToolUse` cannot block on streaming.** Portkey evaluates output guardrails on the
  assembled response after the stream completes and takes no action on the result. Claude Code
  streams by default, so those events are scored and surfaced but not enforced.
  `UserPromptSubmit` and `PostToolUse` do block. True pre-execution tool blocking needs a
  self-hosted Portkey gateway plugin.
- **Hygiene checks are unreachable from a gateway.** `config.settings`, `cwd` and
  `transcript_path` come from the developer's filesystem.
- **Portkey bypass surfaces:** `/v1/proxy/*` skips all guardrails, and Nitro Mode silently
  disables input guardrails.

## 11. Repo

`PhimmStraiker/portkey-plugin-straiker`, branch `feat/claude-code-parity`.

```
middleware/          the service: app.py, coding_agent.py (pure), detect_client.py, dedup.py
spec/                parity_check.py (19 real fixtures), replay_to_straiker.py
deploy/              deploy.sh, DEPLOYMENT.md (this file), SE_SETUP.md, secrets.env.local (ignored)
```

## 12. Teardown

```bash
aws apprunner delete-service --region us-east-2 --service-arn \
  arn:aws:apprunner:us-east-2:108782090038:service/straiker-portkey-coding-middleware/83ea02c7939d4b6c8f481c39c4de609c
```
