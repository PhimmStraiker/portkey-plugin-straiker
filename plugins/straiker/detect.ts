/**
 * Straiker DefendAI plugin handler. Wires the Portkey plugin contract into
 * Straiker's /api/v1/detect (chatbot) and /api/v1/detect?agentic (agentic)
 * endpoints. Mirrors the dedup, payload and blocking semantics used by the
 * Kong, Azure APIM and LiteLLM integrations.
 *
 * Hook coverage: a single handler answers both beforeRequestHook and
 * afterRequestHook by branching on the `eventType` argument (Aporia's pattern).
 *
 * Block contract: Portkey plugins do not directly emit HTTP 403. The handler
 * returns `verdict: false` plus a `data` payload carrying the score, turn id,
 * phase and message; the operator wires `deny: true` on the guardrail entry
 * in their Portkey config to actually block (Portkey serves HTTP 246 in that
 * case). Operators who want observability-only can leave `deny: false`.
 */

import type {
  PluginContext,
  PluginHandler,
  PluginHandlerResponse,
  PluginParameters,
  HookEventType,
} from '../types';
import { post, HttpError, TimeoutError } from '../utils';
import {
  buildPayload,
  chooseDetectUrl,
  isAgenticContinuation,
  readScore,
  readTurnId,
  responseHasToolCalls,
  type ChatRequestBody,
  type ChatResponseBody,
  type DetectResponse,
} from './helpers';

const DEFAULT_DETECT_URL = 'https://api.prod.straiker.ai/api/v1/detect';

interface StraikerParameters {
  agentic?: boolean;
  threshold?: number;
  source?: string;
  destination?: string;
  timeoutMs?: number;
  failOpen?: boolean;
  credentials?: {
    apiKey?: string;
    detectUrl?: string;
  };
}

const handler: PluginHandler<StraikerParameters> = async (
  context: PluginContext,
  parameters: PluginParameters<StraikerParameters>,
  eventType: HookEventType
): Promise<PluginHandlerResponse> => {
  const agentic = parameters.agentic === true;
  const threshold = typeof parameters.threshold === 'number' ? parameters.threshold : 0.5;
  const source = parameters.source ?? (agentic ? 'portkey-plugin-agentic' : 'portkey-plugin');
  const destination = parameters.destination ?? 'api.openai.com';
  const timeoutMs = typeof parameters.timeoutMs === 'number' ? parameters.timeoutMs : 5000;
  const failOpen = parameters.failOpen === true;

  const apiKey = parameters.credentials?.apiKey;
  if (!apiKey) {
    return {
      error: new Error('Straiker plugin: missing credentials.apiKey'),
      verdict: failOpen,
      data: null,
    };
  }
  const baseUrl = parameters.credentials?.detectUrl ?? DEFAULT_DETECT_URL;
  const detectUrl = chooseDetectUrl(baseUrl, agentic);

  const requestBody = (context.request?.json ?? {}) as ChatRequestBody;
  const responseBody = (context.response?.json ?? null) as ChatResponseBody | null;

  // Streaming responses arrive as SSE chunks; context.response.json is null
  // until the stream completes and the body has been buffered. The Kong and
  // APIM integrations skip post-call on streaming for the same reason.
  if (eventType === 'afterRequestHook' && responseBody === null) {
    return { error: null, verdict: true, data: { skipped: 'streaming-response' } };
  }

  // Agentic dedup. One detect call per logical user prompt regardless of
  // how many tool-calling iterations the agent runs.
  if (agentic) {
    if (eventType === 'beforeRequestHook' && isAgenticContinuation(requestBody.messages)) {
      return { error: null, verdict: true, data: { skipped: 'agentic-continuation' } };
    }
    if (eventType === 'afterRequestHook' && responseHasToolCalls(responseBody ?? undefined)) {
      return { error: null, verdict: true, data: { skipped: 'agentic-intermediate-iteration' } };
    }
  }

  const metadata = readContextMetadata(context);

  const payload = buildPayload({
    agentic,
    hook: eventType === 'beforeRequestHook' ? 'pre_call' : 'post_call',
    request: requestBody,
    response: responseBody ?? undefined,
    source,
    destination,
    metadata,
  });

  let detectResponse: DetectResponse | undefined;
  try {
    detectResponse = await post<DetectResponse>(
      detectUrl,
      payload,
      {
        headers: {
          Authorization: `Bearer ${apiKey}`,
          'Content-Type': 'application/json',
        },
      },
      timeoutMs
    );
  } catch (err) {
    if (err instanceof TimeoutError || err instanceof HttpError) {
      return {
        error: err,
        verdict: failOpen,
        data: { straikerUnavailable: true, reason: err.message, failOpen },
      };
    }
    return {
      error: err,
      verdict: failOpen,
      data: { straikerUnavailable: true, failOpen },
    };
  }

  const score = readScore(detectResponse);
  const turnId = readTurnId(detectResponse);
  const phase = eventType === 'beforeRequestHook' ? 'pre_call' : 'post_call';
  const blocked = score > threshold;

  return {
    error: null,
    verdict: !blocked,
    data: {
      provider: 'straiker',
      phase,
      score,
      threshold,
      turn_id: turnId,
      agentic,
      message: blocked
        ? `Straiker: threat detected (${phase}). Set deny=true on this guardrail entry to block.`
        : `Straiker: clean (${phase}).`,
    },
  };
};

interface PluginContextMetadataInput {
  sessionId: string;
  userName: string;
  userRole: string;
  traceId: string;
  agentRole: string;
  ip: string;
  userAgent: string;
}

function readContextMetadata(context: PluginContext): PluginContextMetadataInput {
  const meta = (context.metadata ?? {}) as Record<string, string | undefined>;
  const requestId = (context as { requestId?: string }).requestId;
  return {
    sessionId: pickString(meta, ['session_id', 'sessionId', 'x-session-id']) ?? requestId ?? cryptoRandom(),
    userName: pickString(meta, ['user_name', 'userName', 'user', 'x-user-name']) ?? 'portkey',
    userRole: pickString(meta, ['user_role', 'userRole', 'x-user-role']) ?? 'public',
    traceId: pickString(meta, ['trace_id', 'traceId', 'x-trace-id']) ?? '',
    agentRole: pickString(meta, ['agent_role', 'agentRole', 'x-agent-role']) ?? '',
    ip: pickString(meta, ['ip', 'remote_ip']) ?? '127.0.0.1',
    userAgent: pickString(meta, ['user_agent', 'user-agent', 'userAgent']) ?? 'portkey',
  };
}

function pickString(
  obj: Record<string, string | undefined>,
  keys: string[]
): string | undefined {
  for (const k of keys) {
    const v = obj[k];
    if (typeof v === 'string' && v.length > 0) return v;
  }
  return undefined;
}

function cryptoRandom(): string {
  // Cheap fallback session id when no upstream id is available. The Portkey
  // request id is normally present, this is just defensive.
  return 'straiker-' + Math.random().toString(36).slice(2, 10);
}

export const detect = handler;
export default handler;
