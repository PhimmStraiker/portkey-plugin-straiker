/**
 * Helpers shared between the Straiker plugin's beforeRequestHook and
 * afterRequestHook paths. Ports the payload-building, tool-call reshape and
 * agent-loop dedup logic used by the Kong, Azure APIM and LiteLLM
 * integrations so all four gateways produce semantically identical detect
 * payloads.
 */

export interface OpenAIToolCallFunction {
  name?: string;
  arguments?: string;
}

export interface OpenAIToolCall {
  id?: string;
  function?: OpenAIToolCallFunction;
  name?: string;
  input?: unknown;
}

export interface OpenAIMessage {
  role: string;
  content?: string | Array<{ type: string; text?: string }> | null;
  tool_calls?: OpenAIToolCall[];
  tool_call_id?: string;
  name?: string;
  tool_name?: string;
}

export interface ChatRequestBody {
  model?: string;
  messages?: OpenAIMessage[];
  user?: string;
  stream?: boolean;
}

export interface ChatResponseBody {
  choices?: Array<{
    message?: OpenAIMessage;
    finish_reason?: string;
  }>;
}

export interface DetectMetadata {
  session_id: string;
  user_name: string;
  user_role: string;
  remote_ip: string;
  app_name: string;
  source: string;
  trace_id: string;
  agent_role: string;
}

export interface DetectAnnotations {
  source: string;
  model: string;
  hook: 'pre_call' | 'post_call';
  trace_id: string;
  agent_role: string;
}

export interface AgenticToolCallEntry {
  id?: string;
  name?: string;
  input?: unknown;
}

export interface AgenticMessage {
  role: string;
  content?: string;
  tool_calls?: AgenticToolCallEntry[];
  tool_call_id?: string;
  tool_name?: string;
}

export interface ChatbotPayload {
  prompt: string;
  app_response: string;
  rag_content: string;
  session_id: string;
  user_name: string;
  user_role: string;
  metadata: DetectMetadata;
  network: { IP: string; 'User-Agent': string; 'Content-Type': string };
  annotations: DetectAnnotations;
}

export interface AgenticPayload {
  source: string;
  destination: string;
  messages: AgenticMessage[];
  session_id: string;
  user_name: string;
  user_role: string;
  metadata: DetectMetadata;
  network: { IP: string; 'User-Agent': string; 'Content-Type': string };
  annotations: DetectAnnotations;
}

export type DetectPayload = ChatbotPayload | AgenticPayload;

export interface DetectResponse {
  score?: number;
  turn_id?: string;
  turnId?: string;
  [key: string]: unknown;
}

export type HookEventType = 'beforeRequestHook' | 'afterRequestHook';

/**
 * Pull the most recent user-authored text out of an OpenAI messages[] array.
 * Mirrors Kong handler.lua:extract_user_prompt and the APIM inbound fragment.
 */
export function extractUserPrompt(messages: OpenAIMessage[] | undefined): string {
  if (!messages || messages.length === 0) return '';
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role !== 'user') continue;
    return extractText(m.content);
  }
  return '';
}

export function extractText(content: OpenAIMessage['content']): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    for (const part of content) {
      if (part && part.type === 'text' && typeof part.text === 'string') {
        return part.text;
      }
    }
  }
  return '';
}

/**
 * Reshape OpenAI's tool_calls (function.arguments is a JSON string) into
 * Straiker's flat agentic shape ({id, name, input: <object>}). Ported from
 * Kong handler.lua lines 37-65 and the APIM inbound fragment.
 */
export function transformToolCalls(toolCalls: OpenAIToolCall[] | undefined): AgenticToolCallEntry[] {
  if (!toolCalls) return [];
  const out: AgenticToolCallEntry[] = [];
  for (const tc of toolCalls) {
    const item: AgenticToolCallEntry = { id: tc.id };
    const fn = tc.function;
    if (fn) {
      item.name = fn.name;
      const args = fn.arguments;
      if (typeof args === 'string') {
        try {
          item.input = JSON.parse(args);
        } catch {
          item.input = { _raw: args };
        }
      } else if (args && typeof args === 'object') {
        item.input = args;
      }
    } else {
      item.name = tc.name;
      item.input = tc.input;
    }
    out.push(item);
  }
  return out;
}

/**
 * Convert an OpenAI messages[] array (plus an optional final assistant
 * response) to the flat agentic schema Straiker /detect?agentic expects.
 */
export function buildAgenticMessages(
  messages: OpenAIMessage[] | undefined,
  finalAssistantContent?: string
): AgenticMessage[] {
  const out: AgenticMessage[] = [];
  if (messages) {
    for (const m of messages) {
      const entry: AgenticMessage = { role: m.role };
      const text = extractText(m.content);
      if (text) entry.content = text;
      if (m.tool_calls && m.tool_calls.length > 0) {
        entry.tool_calls = transformToolCalls(m.tool_calls);
      }
      if (m.tool_call_id) entry.tool_call_id = m.tool_call_id;
      if (m.tool_name) entry.tool_name = m.tool_name;
      else if (m.name) entry.tool_name = m.name;
      out.push(entry);
    }
  }
  if (finalAssistantContent) {
    out.push({ role: 'assistant', content: finalAssistantContent });
  }
  return out;
}

/**
 * Agent-loop dedup. Skip pre-call when the last message is a tool/assistant
 * continuation (the agent is iterating, not starting a new turn). Identical
 * rule to Kong/APIM/LiteLLM.
 */
export function isAgenticContinuation(messages: OpenAIMessage[] | undefined): boolean {
  if (!messages || messages.length === 0) return false;
  const last = messages[messages.length - 1];
  return last.role === 'tool' || last.role === 'assistant';
}

/**
 * Skip post-call when the response carries tool_calls (intermediate iteration
 * in the agent loop, not a final assistant answer).
 */
export function responseHasToolCalls(response: ChatResponseBody | undefined): boolean {
  const msg = response?.choices?.[0]?.message;
  return Array.isArray(msg?.tool_calls) && (msg!.tool_calls!.length > 0);
}

export function getAssistantContent(response: ChatResponseBody | undefined): string {
  return extractText(response?.choices?.[0]?.message?.content ?? '');
}

export interface BuildPayloadInput {
  agentic: boolean;
  hook: 'pre_call' | 'post_call';
  request: ChatRequestBody;
  response?: ChatResponseBody;
  source: string;
  destination: string;
  metadata: { sessionId: string; userName: string; userRole: string; traceId: string; agentRole: string; ip: string; userAgent: string };
}

export function buildPayload(input: BuildPayloadInput): DetectPayload {
  const { agentic, hook, request, response, source, destination, metadata } = input;
  const model = typeof request?.model === 'string' ? request.model : 'unknown';

  const network = {
    IP: metadata.ip,
    'User-Agent': metadata.userAgent,
    'Content-Type': 'application/json',
  };
  const meta: DetectMetadata = {
    session_id: metadata.sessionId,
    user_name: metadata.userName,
    user_role: metadata.userRole,
    remote_ip: metadata.ip,
    app_name: source,
    source: 'portkey-plugin',
    trace_id: metadata.traceId,
    agent_role: metadata.agentRole,
  };
  const annotations: DetectAnnotations = {
    source: 'portkey-plugin',
    model,
    hook,
    trace_id: metadata.traceId,
    agent_role: metadata.agentRole,
  };

  if (agentic) {
    const finalAssistant = hook === 'post_call' ? getAssistantContent(response) : undefined;
    return {
      source,
      destination,
      messages: buildAgenticMessages(request.messages, finalAssistant),
      session_id: metadata.sessionId,
      user_name: metadata.userName,
      user_role: metadata.userRole,
      metadata: meta,
      network,
      annotations,
    };
  }

  const prompt = extractUserPrompt(request.messages);
  const appResponse = hook === 'post_call' ? getAssistantContent(response) : 'N/A';
  return {
    prompt,
    app_response: appResponse,
    rag_content: 'N/A',
    session_id: metadata.sessionId,
    user_name: metadata.userName,
    user_role: metadata.userRole,
    metadata: meta,
    network,
    annotations,
  };
}

export function chooseDetectUrl(baseUrl: string, agentic: boolean): string {
  if (!agentic) return baseUrl;
  return baseUrl + (baseUrl.includes('?') ? '&agentic' : '?agentic');
}

export function readScore(body: DetectResponse | undefined): number {
  if (!body) return 0;
  const raw = body.score;
  return typeof raw === 'number' ? raw : 0;
}

export function readTurnId(body: DetectResponse | undefined): string {
  if (!body) return '';
  return (body.turn_id ?? body.turnId ?? '') as string;
}
