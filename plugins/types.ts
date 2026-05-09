/**
 * Minimal stubs of the Portkey gateway plugin types so this repo compiles
 * and tests run in isolation. When the plugin is dropped into a Portkey
 * checkout, Portkey's own `plugins/types.ts` is the canonical source — the
 * real types are a superset of these. Keep these intentionally narrow so any
 * drift from upstream surfaces as a compile error inside Portkey.
 */

export type HookEventType = 'beforeRequestHook' | 'afterRequestHook';

export interface PluginContext {
  request?: { json?: unknown; text?: string };
  response?: { json?: unknown; text?: string };
  metadata?: Record<string, string | undefined>;
  requestType?: string;
  provider?: string;
  requestId?: string;
}

export type PluginParameters<P = Record<string, unknown>> = P & {
  credentials?: Record<string, unknown>;
};

export interface PluginHandlerResponse {
  error: unknown;
  verdict?: boolean;
  data?: unknown;
  transformedData?: { request?: { json: unknown }; response?: { json: unknown } };
  transformed?: boolean;
}

export interface PluginHandlerOptions {
  env?: Record<string, string | undefined>;
  getFromCacheByKey?: (key: string) => Promise<unknown>;
  putInCacheWithValue?: (key: string, value: unknown, ttlSeconds?: number) => Promise<void>;
}

export type PluginHandler<P = Record<string, unknown>> = (
  context: PluginContext,
  parameters: PluginParameters<P>,
  eventType: HookEventType,
  options?: PluginHandlerOptions
) => Promise<PluginHandlerResponse>;
