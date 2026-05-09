/**
 * Unit tests for the Straiker Portkey plugin handler. Stubs the Portkey
 * `post` helper so the tests don't hit the real /detect endpoint.
 *
 * Run: `npm test` from the repo root after `npm install`.
 */

import { describe, it, expect, beforeEach, jest } from '@jest/globals';

const postMock = jest.fn() as jest.Mock<(...args: any[]) => Promise<any>>;

jest.mock('../utils', () => ({
  post: postMock,
  HttpError: class HttpError extends Error {},
  TimeoutError: class TimeoutError extends Error {},
}));

import handler from './detect';

const baseCredentials = { apiKey: 'sk-test', detectUrl: 'https://api.example/detect' };

function makeContext(overrides: Record<string, unknown> = {}) {
  return {
    request: { json: { model: 'gpt-4o-mini', messages: [{ role: 'user', content: 'hi' }] } },
    response: { json: null },
    metadata: { session_id: 'sess-1', user_name: 'alice', trace_id: 'trc-1' },
    requestId: 'req-1',
    ...overrides,
  } as any;
}

describe('Straiker Portkey plugin', () => {
  beforeEach(() => {
    postMock.mockReset();
  });

  it('returns verdict=true on benign pre-call', async () => {
    postMock.mockResolvedValueOnce({ score: 0.1, turn_id: 'pf-1' });
    const res = await handler(
      makeContext(),
      { credentials: baseCredentials } as any,
      'beforeRequestHook'
    );
    expect(res.verdict).toBe(true);
    expect((res.data as any).score).toBe(0.1);
    expect((res.data as any).phase).toBe('pre_call');
    expect(postMock).toHaveBeenCalledTimes(1);
    expect(postMock.mock.calls[0][0]).toBe('https://api.example/detect');
  });

  it('returns verdict=false when score exceeds threshold', async () => {
    postMock.mockResolvedValueOnce({ score: 0.9, turn_id: 'pf-2' });
    const res = await handler(
      makeContext(),
      { credentials: baseCredentials, threshold: 0.5 } as any,
      'beforeRequestHook'
    );
    expect(res.verdict).toBe(false);
    expect((res.data as any).score).toBe(0.9);
    expect((res.data as any).turn_id).toBe('pf-2');
  });

  it('hits /detect?agentic when agentic=true', async () => {
    postMock.mockResolvedValueOnce({ score: 0.0, turn_id: 'pf-3' });
    await handler(
      makeContext(),
      { credentials: baseCredentials, agentic: true } as any,
      'beforeRequestHook'
    );
    expect(postMock.mock.calls[0][0]).toBe('https://api.example/detect?agentic');
    const payload = postMock.mock.calls[0][1] as any;
    expect(payload.source).toBe('portkey-plugin-agentic');
    expect(Array.isArray(payload.messages)).toBe(true);
  });

  it('skips pre-call on agentic continuation (last role = tool)', async () => {
    const ctx = makeContext({
      request: {
        json: {
          model: 'gpt-4o-mini',
          messages: [
            { role: 'user', content: 'hi' },
            { role: 'assistant', content: null, tool_calls: [{ id: 't1', function: { name: 'rag', arguments: '{}' } }] },
            { role: 'tool', tool_call_id: 't1', content: 'result' },
          ],
        },
      },
    });
    const res = await handler(ctx, { credentials: baseCredentials, agentic: true } as any, 'beforeRequestHook');
    expect(res.verdict).toBe(true);
    expect((res.data as any).skipped).toBe('agentic-continuation');
    expect(postMock).not.toHaveBeenCalled();
  });

  it('skips post-call on agentic intermediate iteration (response has tool_calls)', async () => {
    const ctx = makeContext({
      response: {
        json: {
          choices: [
            {
              message: {
                role: 'assistant',
                content: null,
                tool_calls: [{ id: 't1', function: { name: 'rag', arguments: '{"q":"x"}' } }],
              },
            },
          ],
        },
      },
    });
    const res = await handler(ctx, { credentials: baseCredentials, agentic: true } as any, 'afterRequestHook');
    expect(res.verdict).toBe(true);
    expect((res.data as any).skipped).toBe('agentic-intermediate-iteration');
    expect(postMock).not.toHaveBeenCalled();
  });

  it('reshapes function.arguments JSON string to {input: object} for agentic', async () => {
    postMock.mockResolvedValueOnce({ score: 0.0, turn_id: 'pf-4' });
    const ctx = makeContext({
      request: {
        json: {
          model: 'gpt-4o-mini',
          messages: [
            { role: 'user', content: 'find Acme' },
            {
              role: 'assistant',
              content: null,
              tool_calls: [{ id: 't1', function: { name: 'rag_search', arguments: '{"query":"Acme"}' } }],
            },
          ],
        },
      },
      response: {
        json: { choices: [{ message: { role: 'assistant', content: 'Acme is...' } }] },
      },
    });
    await handler(ctx, { credentials: baseCredentials, agentic: true } as any, 'afterRequestHook');
    const payload = postMock.mock.calls[0][1] as any;
    const assistantTurn = payload.messages.find((m: any) => Array.isArray(m.tool_calls));
    expect(assistantTurn.tool_calls[0].input).toEqual({ query: 'Acme' });
    expect(assistantTurn.tool_calls[0].name).toBe('rag_search');
  });

  it('fails open when failOpen=true and Straiker errors', async () => {
    postMock.mockRejectedValueOnce(new Error('upstream blew up'));
    const res = await handler(
      makeContext(),
      { credentials: baseCredentials, failOpen: true } as any,
      'beforeRequestHook'
    );
    expect(res.verdict).toBe(true);
    expect((res.data as any).straikerUnavailable).toBe(true);
  });

  it('fails closed when failOpen=false and Straiker errors', async () => {
    postMock.mockRejectedValueOnce(new Error('upstream blew up'));
    const res = await handler(
      makeContext(),
      { credentials: baseCredentials, failOpen: false } as any,
      'beforeRequestHook'
    );
    expect(res.verdict).toBe(false);
    expect((res.data as any).straikerUnavailable).toBe(true);
  });

  it('skips streaming responses on afterRequestHook', async () => {
    const ctx = makeContext({ response: { json: null } });
    const res = await handler(ctx, { credentials: baseCredentials } as any, 'afterRequestHook');
    expect(res.verdict).toBe(true);
    expect((res.data as any).skipped).toBe('streaming-response');
    expect(postMock).not.toHaveBeenCalled();
  });
});
