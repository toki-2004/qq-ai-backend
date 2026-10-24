import http from 'node:http';
import { URL } from 'node:url';
import fs from 'node:fs/promises';
import { config } from './config.js';
import { DeepSeekClient } from './deepseekClient.js';
import { logger } from './logger.js';
import { Mutex } from './mutex.js';
import { parseChatRequest, cleanupFiles } from './parser.js';
import { listModels, resolveModel } from './models.js';
import {
  ApiError,
  completionResponse,
  contentChunk,
  done,
  error,
  heartbeat,
  json,
  requestId,
  roleChunk,
  sse,
  sseHeaders
} from './http.js';

const client = new DeepSeekClient(config);
const mutex = new Mutex();

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);

if (config.loginMode) {
  await client.login();
} else {
  await fs.mkdir(config.tempDir, { recursive: true });
  const server = http.createServer(handleRequest);
  server.listen(config.port, config.host, () => {
    logger.info('DeepSeekWeb2API started', {
      url: config.publicBaseUrl || `http://${config.host}:${config.port}`,
      auth: config.apiKey ? 'enabled' : 'disabled'
    });
  });
  // 起服务就顺手确认一次登录态（后台，不挡端口就绪）：登录失效要早发现，
  // 不要让用户聊到一半才发现进不去对话界面；结果从 /health 的 loggedIn 读。
  // 检查也走同一个队列：否则它和启动后马上来的第一条请求同时 page.goto，
  // 后到的那次导航会被前一次中断（net::ERR_ABORTED / context closed）。
  mutex.run(() => client.checkSession())
    .then(ok => logger.info('session check finished', { loggedIn: ok }))
    .catch(err => logger.warn('session check error', { error: err.message }));
  // 闲置就把浏览器关掉：无头 Chromium 一套 8 个进程、几百 MB，桌宠大部分时间用不到它；
  // 下次请求会按需重开，并回到"本次进程自己创建的那段对话"（见 ensureConversationPage）。
  if (config.browserIdleMs > 0) {
    const idleTimer = setInterval(() => {
      if (!client.context || client.busy || mutex.size > 0) return;
      if (Date.now() - client.lastUse < config.browserIdleMs) return;
      logger.info('closing idle browser', { idleSeconds: Math.round(config.browserIdleMs / 1000) });
      client.close().catch(() => {});
    }, 60000);
    idleTimer.unref?.();
  }
}

async function shutdown() {
  logger.info('shutting down');
  await client.close();
  process.exit(0);
}

async function handleRequest(req, res) {
  const parsedUrl = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`);

  try {
    if (req.method === 'GET' && parsedUrl.pathname === '/health') {
      // ?check=1：同步做一次"能不能进对话界面"的检查（并发时复用同一次检查）
      if (parsedUrl.searchParams.get('check') === '1') {
        await mutex.run(() => client.checkSession()).catch(() => {});
      }
      json(res, 200, { ok: true, queue: mutex.size, loggedIn: client.loggedIn,
                       conversation: client.currentConversationId || '' });
      return;
    }

    if (parsedUrl.pathname.startsWith('/v1/')) {
      authorize(req);
    }

    if (req.method === 'GET' && parsedUrl.pathname === '/v1/models') {
      json(res, 200, listModels());
      return;
    }

    if (req.method === 'POST' && parsedUrl.pathname === '/v1/chat/completions') {
      await handleChatCompletions(req, res);
      return;
    }

    json(res, 404, { error: { message: 'Not found', type: 'invalid_request_error', code: 'not_found' } });
  } catch (err) {
    logger.error('request failed', { error: err.message });
    error(res, err);
  }
}

function authorize(req) {
  if (!config.apiKey) return;
  const header = req.headers.authorization || '';
  const token = header.startsWith('Bearer ') ? header.slice(7) : '';
  if (token !== config.apiKey) {
    throw new ApiError('Invalid API key', 401, 'invalid_api_key', 'authentication_error');
  }
}

async function readJson(req) {
  const chunks = [];
  let total = 0;

  for await (const chunk of req) {
    total += chunk.length;
    if (total > config.maxBodyBytes) {
      throw new ApiError('Request body is too large', 413, 'request_too_large', 'invalid_request_error');
    }
    chunks.push(chunk);
  }

  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch {
    throw new ApiError('Request body must be valid JSON', 400, 'invalid_json', 'invalid_request_error');
  }
}

async function handleChatCompletions(req, res) {
  const body = await readJson(req);
  const parsed = await parseChatRequest(body, config);
  const model = resolveModel(parsed.requestedModel);
  if (!model) {
    await cleanupFiles(parsed.imagePaths);
    throw new ApiError(`Model is not supported: ${parsed.requestedModel}`, 400, 'model_not_found', 'invalid_request_error');
  }

  const id = requestId();
  if (parsed.stream) {
    await handleStreaming(res, id, model.id, async onDelta => {
      try {
        return await client.generate({
          prompt: parsed.prompt,
          imagePaths: parsed.imagePaths,
          model,
          onDelta,
          sessionId: parsed.sessionId
        });
      } finally {
        await cleanupFiles(parsed.imagePaths);
      }
    });
    return;
  }

  const run = async () => {
    try {
      return await client.generate({
        prompt: parsed.prompt,
        imagePaths: parsed.imagePaths,
        model,
        sessionId: parsed.sessionId
      });
    } finally {
      await cleanupFiles(parsed.imagePaths);
    }
  };

  const result = await mutex.run(run);
  json(res, 200, completionResponse({
    id,
    model: model.id,
    content: result.text,
    reasoning: result.reasoning
  }));
}

async function handleStreaming(res, id, modelId, runWithDelta) {
  sseHeaders(res);
  sse(res, roleChunk({ id, model: modelId }));
  const keepalive = setInterval(() => heartbeat(res), config.keepaliveMs);
  let emitted = false;

  const onDelta = delta => {
    if (res.writableEnded || !delta?.value) return;
    emitted = true;
    if (delta.type === 'reasoning') {
      sse(res, contentChunk({ id, model: modelId, reasoning: delta.value }));
    } else {
      sse(res, contentChunk({ id, model: modelId, content: delta.value }));
    }
  };

  try {
    const result = await mutex.run(() => runWithDelta(onDelta));
    if (!emitted && result.reasoning) {
      sse(res, contentChunk({ id, model: modelId, reasoning: result.reasoning }));
    }
    if (!emitted && result.text) {
      sse(res, contentChunk({ id, model: modelId, content: result.text }));
    }
    sse(res, contentChunk({ id, model: modelId, finishReason: 'stop' }));
    done(res);
  } catch (err) {
    error(res, err, true);
  } finally {
    clearInterval(keepalive);
  }
}
