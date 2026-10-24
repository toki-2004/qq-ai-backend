import crypto from 'node:crypto';

export class ApiError extends Error {
  constructor(message, status = 500, code = 'internal_error', type = 'server_error') {
    super(message);
    this.status = status;
    this.code = code;
    this.type = type;
  }
}

export function requestId() {
  return `chatcmpl-${crypto.randomUUID()}`;
}

export function nowSeconds() {
  return Math.floor(Date.now() / 1000);
}

export function json(res, status, payload) {
  if (res.writableEnded) return;
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store'
  });
  res.end(JSON.stringify(payload));
}

export function error(res, err, stream = false) {
  const status = err instanceof ApiError ? err.status : 500;
  const payload = {
    error: {
      message: err?.message || 'Internal error',
      type: err?.type || 'server_error',
      code: err?.code || 'internal_error'
    }
  };

  if (stream) {
    sse(res, payload);
    done(res);
    return;
  }

  json(res, status, payload);
}

export function sseHeaders(res) {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no'
  });
}

export function sse(res, payload) {
  if (res.writableEnded) return;
  res.write(`data: ${JSON.stringify(payload)}\n\n`);
}

export function heartbeat(res) {
  if (res.writableEnded) return;
  res.write(':keepalive\n\n');
}

export function done(res) {
  if (res.writableEnded) return;
  res.write('data: [DONE]\n\n');
  res.end();
}

export function completionResponse({ id, model, content, reasoning }) {
  const message = { role: 'assistant', content };
  if (reasoning) message.reasoning_content = reasoning;
  return {
    id,
    object: 'chat.completion',
    created: nowSeconds(),
    model,
    choices: [{ index: 0, message, finish_reason: 'stop' }]
  };
}

export function roleChunk({ id, model }) {
  return {
    id,
    object: 'chat.completion.chunk',
    created: nowSeconds(),
    model,
    choices: [{ index: 0, delta: { role: 'assistant' }, finish_reason: null }]
  };
}

export function contentChunk({ id, model, content, reasoning, finishReason = null }) {
  const delta = {};
  if (content) delta.content = content;
  if (reasoning) delta.reasoning_content = reasoning;
  return {
    id,
    object: 'chat.completion.chunk',
    created: nowSeconds(),
    model,
    choices: [{ index: 0, delta, finish_reason: finishReason }]
  };
}
