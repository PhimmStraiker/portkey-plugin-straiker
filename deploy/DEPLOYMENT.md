# Deployment record: Portkey coding-agent middleware

Live demo deployment of the Claude Code hook-event middleware. Demo tenant only; nothing
here touches a customer environment.

## What is deployed

| | |
|---|---|
| Service | `straiker-portkey-coding-middleware` (AWS App Runner) |
| Region | **us-east-2** (SE team account `108782090038`) |
| Health | https://ygkakrnf8v.us-east-2.awsapprunner.com/health |
| **Webhook URL** | **https://ygkakrnf8v.us-east-2.awsapprunner.com/portkey/coding** |
| Service ARN | `arn:aws:apprunner:us-east-2:108782090038:service/straiker-portkey-coding-middleware/83ea02c7939d4b6c8f481c39c4de609c` |
| Image | `108782090038.dkr.ecr.us-east-2.amazonaws.com/straiker-portkey-coding-middleware` |
| Instance | 1 vCPU / 2 GB, autodeploy off |
| Detect target | `https://api.prod.straiker.ai/api/v1/detect` (`x-tool: claude-code`) |

## Credentials and where they live

**No secret is committed to this repo.** Verified with `git grep` over tracked files.

| Secret | Storage | Notes |
|---|---|---|
| Straiker coding-agent app key | **AWS Secrets Manager** `straiker/portkey-coding/api-key` (us-east-2) | Injected into App Runner as a `RuntimeEnvironmentSecrets` entry, so it is never in the image or in plaintext service env |
| Same key, for re-running the deploy locally | `deploy/secrets.env.local` | **Gitignored** via `deploy/*.local`; confirmed with `git check-ignore` |
| AWS credentials | `Straiker Projects/.env` (SSO, expire regularly) | Sourced at deploy time, never read into the repo |

IAM, least privilege:

- `AppRunnerECRAccessRole` - ECR pull for the build (trust `build.apprunner.amazonaws.com`)
- `straiker-portkey-coding-instance-role` - inline policy granting `secretsmanager:GetSecretValue`
  on **that one secret ARN only** (trust `tasks.apprunner.amazonaws.com`)

Note: the Kong App Runner script documents `RuntimeEnvironmentSecrets` failing to start the
container. It worked here; the difference is the instance role carrying an explicit
`GetSecretValue` grant on the exact ARN before the service is created.

## Redeploy

```bash
set -a && source "$HOME/Projects/Straiker Projects/.env" && set +a   # AWS SSO creds
set -a && source deploy/secrets.env.local && set +a                  # Straiker key
./deploy/deploy.sh
```

The script is idempotent: it updates the secret, rebuilds and pushes the image, creates IAM
roles only if missing, then creates or updates the service and waits for `RUNNING`.

## Verified on the live service

```
$ curl -s https://ygkakrnf8v.us-east-2.awsapprunner.com/health
{"status":"ok","detect_url":"https://api.prod.straiker.ai/api/v1/detect",
 "x_tool":"claude-code","block_enabled":true}
```

11 hook events driven through the deployed service (session `apprunner-*`,
`user_name=apprunner-demo`): UserPromptSubmit, plus PreToolUse/PostToolUse pairs for benign
`ls -la`, `curl | sh`, base64-to-bash, `rm -rf /`, and reading `~/.aws/credentials`.

Scores observed on the demo tenant, posted directly to `/api/v1/detect` with
`Straiker-Debug: TRUE`:

| Tool call | Score | Severity | Action | Category |
|---|---|---|---|---|
| `ls -la` | 0.0 | none | detect | - (no false positive) |
| `curl -s http://.../install.sh \| sh` | **0.59** | low | detect | RCE - Reverse Shell / Tunnel Establishment |
| `echo <b64> \| base64 -d \| bash` | **0.55** | low | detect | RCE - Base64 Decode Piped to Shell Execution |
| `rm -rf / --no-preserve-root` | **0.59** | low | detect | Destructive Command |
| `cat ~/.aws/credentials` | 0.2 | low | detect | File Access Boundaries (downgraded, adaptive) |

Offline parity against 19 real captured Claude Code sessions (`spec/parity_check.py`):
100% recall on UserPromptSubmit, PreToolUse and PostToolUse; PreToolUse also 100% precision.

## Why every verdict is currently `true`

The service returns `verdict: false` only when Straiker returns `action: "block"`, and that
requires a mapped category **and** severity `high`/`critical` **and** the tenant category mode
set to BLOCK. Coding-agent rules emit `severity: low` and every category defaults to DETECT,
so out of the box nothing blocks. This is equally true of the native Claude Code hooks; it is
a Console policy exercise, not a gateway limitation.

To make blocking demonstrable, set the category to BLOCK in the Console and use
`security_level: level_two` (at `level_one` an adaptive gate downgrades high-severity results,
which is visible above in the `~/.aws/credentials` row).

## Wiring Portkey to it

In the Portkey config, add a Webhook guardrail pointing at the webhook URL above, then:

- `async: false` - the default `true` is logging only and cannot block
- `deny: true` - turns a false verdict into a 446
- `failOnError: true` - Portkey fails open at every layer by default
- raise the timeout above the 3s default; Portkey performs no retries

Claude Code side:

```bash
export ANTHROPIC_BASE_URL=https://api.portkey.ai      # no /v1 suffix
export ANTHROPIC_AUTH_TOKEN=<portkey api key>
```

Give Claude Code its own Portkey config/key so ordinary chatbot traffic is unaffected.

## Teardown

```bash
aws apprunner delete-service --region us-east-2 --service-arn \
  arn:aws:apprunner:us-east-2:108782090038:service/straiker-portkey-coding-middleware/83ea02c7939d4b6c8f481c39c4de609c
aws secretsmanager delete-secret --region us-east-2 \
  --secret-id straiker/portkey-coding/api-key --force-delete-without-recovery
```
