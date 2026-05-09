# Install the Straiker plugin into a Portkey AI Gateway

The Portkey AI Gateway compiles plugins into the runtime, so installing the Straiker plugin means dropping the source into a Portkey checkout and rebuilding. There is no sidecar / no hot-reload path today.

## Prerequisites

- A clone or fork of [Portkey-AI/gateway](https://github.com/Portkey-AI/gateway).
- Node 18+ and `npm`.
- A Straiker app + API key. The app's name (set in the Straiker Console) is what you put under `parameters.source` in your client config.

## Step 1 — Drop the plugin source into the Portkey gateway repo

```bash
cd /path/to/portkey-ai-gateway
mkdir -p plugins/straiker
cp -R /path/to/portkey-plugin-straiker/plugins/straiker/* plugins/straiker/
```

You should now have:

```
plugins/
  default/
  pillar/
  aporia/
  straiker/                    ← new
    manifest.json
    detect.ts
    helpers.ts
    straiker.test.ts
```

## Step 2 — Enable the plugin and add credentials

Edit `conf.json` at the root of the gateway repo:

```json
{
  "plugins_enabled": ["default", "straiker"],
  "credentials": {
    "straiker": {
      "apiKey": "<your-straiker-app-key>",
      "detectUrl": "https://api.prod.straiker.ai/api/v1/detect"
    }
  }
}
```

A reference `examples/conf.json` is included in this repo.

## Step 3 — Build and start the gateway

```bash
npm install
npm run build-plugins
npm run dev          # or your usual production start command
```

Verify the plugin shows up in the gateway logs at startup.

## Step 4 — Reference the plugin in a Portkey config

Add `straiker.detect` to the `before_request_hooks` and / or `after_request_hooks` of any Portkey config. Two reference configs are in `examples/`:

- `client-config-chatbot.json` — single-turn chatbot mode
- `client-config-agentic.json` — multi-turn / tool-calling agentic mode

Each entry takes the same parameters documented in `manifest.json`. The most important knob is `agentic` — set to `true` whenever the request is part of a tool-calling agent loop, otherwise leave it `false`.

## Step 5 — Verify

Send a benign request through the gateway and a Straiker turn should appear in the Console under the application named in `parameters.source`. Send a known-bad prompt and Portkey should serve HTTP 246 (its custom guardrail-failed status) when `deny: true` is set on the guardrail entry. The response body's `data` carries the Straiker `score`, `turn_id`, and `phase`.

## Upgrading

This plugin follows the same payload shape as Kong, APIM and LiteLLM. The `/detect` and `/detect?agentic` contract is stable. Pulling a newer plugin version is just `cp -R` over the existing files and `npm run build-plugins`.
