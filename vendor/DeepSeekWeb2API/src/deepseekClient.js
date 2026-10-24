import fs from 'node:fs/promises';
import { chromium } from 'playwright';
import { ApiError } from './http.js';
import { logger } from './logger.js';

const INPUT_SELECTOR = 'textarea, [contenteditable="true"]';
const SEND_BUTTON_NAMES = /send|发送|提交/i;
const THINK_BUTTON_NAMES = /deepthink|deep think|深度思考|深度/i;
const SEARCH_BUTTON_NAMES = /search|联网搜索|搜索/i;
const VISION_BUTTON_NAMES = /image recognition|vision|识图|图像|图片识别|图片理解/i;

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/* 会话失效判定：用户在网页端（自己的浏览器）删掉当前对话后，这里仍在旧会话页，
   发送必然失败。命中任一信号即认为会话没了，需要自动开新对话：
   1) 完成接口返回 404；
   2) 错误体包含"不存在/已删除/not found"等标记；
   3) 失败后页面 URL 已不含当前会话 id（被应用重定向走）；
   4) 已进入过会话但发送后完全没有产生 completion 请求（页面已失效）。 */
const GONE_MARKERS = /not found|doesn'?t exist|不存在|已删除|已被删除|无效的会话|会话无效|invalid (chat|conversation|session)/i;

export function conversationLooksGone({
  httpStatus = 0,
  errorText = '',
  url = '',
  conversationId = '',
  sawCompletionResponse = true
} = {}) {
  if (httpStatus === 404) return true;
  if (errorText && GONE_MARKERS.test(errorText)) return true;
  if (conversationId && url && !url.includes(conversationId)) return true;
  if (conversationId && !sawCompletionResponse && httpStatus === 0) return true;
  return false;
}

export class DeepSeekClient {
  constructor(config) {
    this.config = config;
    this.context = null;
    this.page = null;
    this.cdp = null;
    this.messagesInConversation = 0;  // 当前对话里本次进程已发的消息数
    this.currentConversationId = null;  // 本次进程自己创建的对话 id（绝不进入其他对话）
    this.loggedIn = null;  // null=还没查过；true/false=最近一次进对话界面的结果
    this.lastUse = Date.now();  // 最近一次用到浏览器（闲置关浏览器用）
    this.busy = false;          // 正在生成：别在这时候关浏览器
  }

  async start() {
    // 先把旧浏览器关掉：页面被关/崩了之后又 start() 会再起一整套 Chromium，
    // 旧的那套没人回收 → 后台越堆越多（一个实例在任务管理器里就是 8 个进程）
    if (this.context) await this.close();
    await fs.mkdir(this.config.userDataDir, { recursive: true });
    const launchOptions = {
      // 服务运行无头（不弹窗口）；登录模式必须可见，否则用户无法操作
      headless: this.config.loginMode ? false : this.config.headless,
      viewport: { width: 1366, height: 900 }
    };
    if (this.config.browserChannel) launchOptions.channel = this.config.browserChannel;
    if (this.config.browserExecutablePath) launchOptions.executablePath = this.config.browserExecutablePath;

    this.context = await chromium.launchPersistentContext(this.config.userDataDir, launchOptions);
    this.page = this.context.pages()[0] || await this.context.newPage();
    this.page.setDefaultTimeout(30000);
    this.page.setDefaultNavigationTimeout(60000);
    this.cdp = await this.context.newCDPSession(this.page).catch(() => null);
    await this.cdp?.send('Network.enable').catch(() => {
      this.cdp = null;
    });
    logger.info('browser started', {
      headless: this.config.headless,
      browser: this.config.browserName || 'playwright-chromium',
      executablePath: this.config.browserExecutablePath || undefined,
      channel: this.config.browserChannel || undefined,
      userDataDir: this.config.userDataDir
    });
  }

  async close() {
    await this.context?.close().catch(() => {});
    this.context = null;
    this.page = null;
    this.cdp = null;
  }

  async login() {
    await this.ensureStarted();
    await this.page.goto(this.config.targetUrl, { waitUntil: 'domcontentloaded' });
    logger.info('login mode: finish login in the opened browser, then press Ctrl+C here');
    await new Promise(() => {});
  }

  async generate({ prompt, imagePaths, model, onDelta }) {
    this.busy = true;
    this.lastUse = Date.now();
    try {
      return await this._generate({ prompt, imagePaths, model, onDelta });
    } finally {
      this.busy = false;
      this.lastUse = Date.now();
    }
  }

  async _generate({ prompt, imagePaths, model, onDelta }) {
    await this.ensureStarted();
    const page = this.page;
    const capabilities = model.capabilities;

    let responseState = null;
    for (let attempt = 0; attempt < 2; attempt++) {
      const startedAt = Date.now();
      try {
        await this.ensureConversationPage();
        await this.waitForComposer();
        await this.configureMode(capabilities, imagePaths.length > 0);
        await this.fillPrompt(prompt);
        if (imagePaths.length > 0) {
          await this.uploadImages(imagePaths);
        }
        responseState = this.createCompletionWatcher({ onDelta });
        await this.submit();

        const result = await this.waitForCompletion(responseState, startedAt);
        if (!result.text && !result.reasoning) {
          throw new ApiError('DeepSeek returned an empty response', 502, 'empty_response', 'server_error');
        }
        this.messagesInConversation += 1;
        const m = /\/a\/chat\/s\/([0-9a-f-]{36})/i.exec(page.url());
        if (m) this.currentConversationId = m[1];
        return result;
      } catch (err) {
        // 疑似登录失效：可能只是页面还在加载/被切走，回首页重新进一次再试；
        // 真的掉登录时第二次仍会失败，那时才把 login_required 抛给上层
        if (err.code === 'login_required' && attempt === 0) {
          logger.warn('login page seen, retrying after reload', { error: err.message });
          this.messagesInConversation = 0;
          this.currentConversationId = null;
          await this.page.goto(this.config.targetUrl, { waitUntil: 'domcontentloaded' }).catch(() => {});
          continue;
        }
        if (err.code === 'login_required') throw err;
        // 会话在网页端被用户删掉等失效场景：自动开新对话重试一次
        if (attempt === 0 && this._conversationLooksGone(responseState)) {
          logger.warn('conversation invalid, retrying in a new conversation', { error: err.message });
          this.messagesInConversation = 0;
          this.currentConversationId = null;
          await this.page.goto(this.config.targetUrl, { waitUntil: 'domcontentloaded' }).catch(() => {});
          continue;
        }
        throw err;
      }
    }
    throw new ApiError('DeepSeek request failed', 502, 'deepseek_error', 'server_error');
  }

  _conversationLooksGone(responseState) {
    const s = (responseState && responseState.state) || {};
    return conversationLooksGone({
      httpStatus: s.httpStatus || 0,
      errorText: s.errorText || s.lastError || '',
      url: this.page ? this.page.url() : '',
      conversationId: this.currentConversationId || '',
      sawCompletionResponse: Boolean(s.sawCompletionResponse)
    });
  }

  /* 会话管理（安全版）：只复用"本次进程自己创建"的对话，绝不删除、绝不进入其他对话。
     - 页面还在自己的对话里：继续复用（不导航，避免每条消息新建对话）；
     - 首次消息 / 触顶 / 页面被切走：回首页开新对话（不删除任何历史）。 */
  async ensureConversationPage() {
    const limit = this.config.newConversationAfter;
    if (limit > 0 && this.messagesInConversation >= limit) {
      await this.page.goto(this.config.targetUrl, { waitUntil: 'domcontentloaded' });
      this.messagesInConversation = 0;
      this.currentConversationId = null;
      return;
    }
    if (this.currentConversationId) {
      if (this.page.url().includes(this.currentConversationId)) {
        return;  // 还在自己的对话里：继续复用
      }
      // 浏览器被闲置关掉/崩溃重启过：回到"本次进程自己创建的那段对话"，
      // 不要另开新的（绝不进入别人的对话，只是找回自己的）
      const back = `${this.config.targetUrl.replace(/\/+$/, '')}/a/chat/s/${this.currentConversationId}`;
      await this.page.goto(back, { waitUntil: 'domcontentloaded' }).catch(() => {});
      if (this.page.url().includes(this.currentConversationId)) return;
      logger.warn('own conversation could not be reopened; starting a new one');
    }
    await this.page.goto(this.config.targetUrl, { waitUntil: 'domcontentloaded' });
    this.messagesInConversation = 0;
    this.currentConversationId = null;
  }

  async ensureStarted() {
    if (!this.context || !this.page || this.page.isClosed()) {
      await this.start();
    }
  }

  async waitForComposer() {
    const page = this.page;
    // 登录态失效时页面会停在登录界面（没有输入框），必须快速失败并给出明确信号，
    // 否则发送流程会一直等到 requestTimeoutMs，用户侧表现为"卡住不动"。
    const deadline = Date.now() + 45000;
    while (Date.now() < deadline) {
      if (await page.locator(INPUT_SELECTOR).first().isVisible().catch(() => false)) {
        this.loggedIn = true;
        return;
      }
      if (await this.loginPageVisible()) {
        // 页面加载过程中可能短暂呈现登录态（文案命中但没真掉登录），
        // 隔 2 秒再确认一次，避免把正常请求误判成登录失效
        await delay(2000);
        if (await this.loginPageVisible()) {
          this.loggedIn = false;
          throw new ApiError(
            'DeepSeek 登录态已失效：页面停在登录界面，请在设置页点"重新绑定"重新登录。',
            401,
            'login_required',
            'authentication_error'
          );
        }
      }
      await delay(500);
    }
    this.loggedIn = false;
    const url = page.url();
    throw new ApiError(
      `DeepSeek composer was not found. The account may not be logged in. Run npm run login first. Current URL: ${url}`,
      401,
      'deepseek_not_logged_in',
      'authentication_error'
    );
  }

  async loginPageVisible() {
    const url = this.page.url();
    if (/sign[_-]?in|log[_-]?in/i.test(url)) return true;
    // 只认"有密码输入框且没有输入框"：光看页面文案在加载/首屏可能误报，
    // 误报的代价是正常请求被当成登录失效（用户只看到突然失败）
    return this.page.evaluate(() => {
      if (document.querySelector('textarea, [contenteditable="true"]')) return false;
      return document.querySelectorAll('input[type="password"]').length > 0;
    }).catch(() => false);
  }

  /* 启动/重连后先确认能进入对话界面（只导航+等输入框，不发消息、不新建对话）。 */
  async checkSession() {
    if (!this.sessionCheck) {
      this.sessionCheck = this._checkSession()
        .finally(() => { this.sessionCheck = null; });
    }
    return this.sessionCheck;  // 并发调用共用同一次检查
  }

  async _checkSession() {
    this.lastUse = Date.now();
    await this.ensureStarted();
    try {
      // 已经停在对话界面（含正在复用的那段对话）就地确认，不导航：
      // 导航会打断正在进行的网页对话，让下一条消息另开新对话。
      if (await this.page.locator(INPUT_SELECTOR).first().isVisible().catch(() => false)) {
        this.loggedIn = true;
        return true;
      }
      await this.page.goto(this.config.targetUrl, { waitUntil: 'domcontentloaded' });
      await this.waitForComposer();
      return true;
    } catch (err) {
      this.loggedIn = false;
      logger.warn('session check failed', { error: err.message, code: err.code || '' });
      return false;
    }
  }

  async configureMode(capabilities, hasImages) {
    if (capabilities.expert) {
      await this.clickFirst([
        'div[data-model-type="expert"]',
        '[data-testid*="expert" i]',
        'button:has-text("Expert")',
        'button:has-text("专业")'
      ]);
    } else {
      await this.clickFirst([
        'div[data-model-type="default"]',
        'div[data-model-type="chat"]',
        'button:has-text("Instant")',
        'button:has-text("Flash")',
        'button:has-text("极速")'
      ]);
    }

    if (capabilities.vision || hasImages) {
      const switched = await this.clickVisionMode();
      if (!switched) {
        logger.warn('vision mode entry not found; continuing with upload fallback');
      }
    }

    await this.setToggle(THINK_BUTTON_NAMES, Boolean(capabilities.thinking));
    await this.setToggle(SEARCH_BUTTON_NAMES, Boolean(capabilities.search));
  }

  async clickVisionMode() {
    const candidates = [
      'div[data-model-type="vision"]',
      'div[data-model-type="image"]',
      'div[data-model-type="multimodal"]',
      '[data-testid*="vision" i]',
      '[data-testid*="image" i]'
    ];
    if (await this.clickFirst(candidates)) return true;

    const roleButton = this.page.getByRole('button', { name: VISION_BUTTON_NAMES }).first();
    if (await roleButton.count().catch(() => 0)) {
      await roleButton.click({ force: true });
      await delay(500);
      return true;
    }

    const textButton = this.page.getByText(VISION_BUTTON_NAMES).first();
    if (await textButton.count().catch(() => 0)) {
      await textButton.click({ force: true });
      await delay(500);
      return true;
    }

    return false;
  }

  async clickFirst(selectors) {
    for (const selector of selectors) {
      const locator = this.page.locator(selector).first();
      if ((await locator.count().catch(() => 0)) === 0) continue;
      try {
        await locator.click({ force: true, timeout: 3000 });
        await delay(400);
        return true;
      } catch {
        continue;
      }
    }
    return false;
  }

  async setToggle(namePattern, targetState) {
    const button = this.page.getByRole('button', { name: namePattern }).first();
    if ((await button.count().catch(() => 0)) === 0) return false;

    const selected = await button.evaluate(element => {
      const ariaPressed = element.getAttribute('aria-pressed');
      if (ariaPressed === 'true') return true;
      if (ariaPressed === 'false') return false;
      const ariaChecked = element.getAttribute('aria-checked');
      if (ariaChecked === 'true') return true;
      if (ariaChecked === 'false') return false;
      return /\b(selected|active|checked)\b/i.test(element.className || '');
    }).catch(() => false);

    if (selected !== targetState) {
      await button.click({ force: true });
      await delay(400);
    }
    return true;
  }

  async uploadImages(imagePaths) {
    const page = this.page;
    const uploadDone = this.waitForUploadResponses(imagePaths.length);
    const fileChooserPromise = page.waitForEvent('filechooser', { timeout: 5000 }).catch(() => null);
    const clicked = await this.clickFirst([
      'button[aria-label*="Attach" i]',
      'button[aria-label*="Upload" i]',
      'button[aria-label*="File" i]',
      'button[aria-label*="上传" i]',
      'button[aria-label*="附件" i]',
      '[data-testid*="upload" i]',
      '[data-testid*="attach" i]'
    ]);

    const fileChooser = clicked ? await fileChooserPromise : null;
    if (fileChooser) {
      await fileChooser.setFiles(imagePaths);
    } else {
      const fileInput = await this.findUsableFileInput();
      if (!fileInput) {
        throw new ApiError('No file upload control was found on DeepSeek Web. The account may not have vision upload enabled.', 502, 'upload_not_available', 'server_error');
      }
      await fileInput.setInputFiles(imagePaths);
    }

    await uploadDone;
    await this.waitForUploadFinished(imagePaths.length);
  }

  waitForUploadResponses(expectedCount) {
    const page = this.page;
    let uploadedCount = 0;

    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        cleanup();
        reject(new ApiError('Timed out waiting for DeepSeek image upload API', 504, 'upload_timeout', 'server_error'));
      }, this.config.uploadTimeoutMs);

      const cleanup = () => {
        clearTimeout(timeout);
        page.off('response', onResponse);
      };

      const onResponse = response => {
        const url = response.url();
        if (!url.includes('/api/v0/file/upload_file')) return;
        if (response.status() >= 400) {
          cleanup();
          reject(new ApiError(`DeepSeek image upload failed: HTTP ${response.status()}`, 502, 'upload_failed', 'server_error'));
          return;
        }

        uploadedCount += 1;
        if (uploadedCount >= expectedCount) {
          cleanup();
          resolve();
        }
      };

      page.on('response', onResponse);
    });
  }

  async findUsableFileInput() {
    const handles = await this.page.locator('input[type="file"]').elementHandles();
    for (const handle of handles.reverse()) {
      const attached = await handle.evaluate(el => el.isConnected && !el.disabled).catch(() => false);
      if (attached) return handle;
    }
    return null;
  }

  async waitForUploadFinished(expectedCount) {
    const page = this.page;
    const started = Date.now();

    while (Date.now() - started < this.config.uploadTimeoutMs) {
      const status = await page.evaluate(() => {
        const text = document.body?.innerText || '';
        const loading = /uploading|processing|上传中|处理中|解析中/i.test(text);
        const failed = /upload failed|上传失败|不支持|unsupported/i.test(text);
        const previews = document.querySelectorAll('img, [data-testid*="file"], [class*="file"], [class*="upload"]').length;
        return { loading, failed, previews };
      }).catch(() => ({ loading: false, failed: false, previews: 0 }));

      if (status.failed) {
        throw new ApiError('DeepSeek reported that image upload failed', 502, 'upload_failed', 'server_error');
      }
      if (!status.loading && status.previews >= expectedCount) return;
      await delay(700);
    }

    logger.warn('image upload wait timed out; continuing because the file input accepted the files');
  }

  async fillPrompt(prompt) {
    const input = this.page.locator(INPUT_SELECTOR).first();
    await input.click({ force: true });
    await input.fill('').catch(async () => {
      const modifier = process.platform === 'darwin' ? 'Meta' : 'Control';
      await this.page.keyboard.press(`${modifier}+A`);
      await this.page.keyboard.press('Backspace');
    });

    if (prompt) {
      await input.fill(prompt).catch(async () => {
        await this.page.evaluate(text => {
          document.execCommand('insertText', false, text);
        }, prompt);
      });
    }
  }

  createCompletionWatcher({ onDelta } = {}) {
    if (onDelta && this.cdp) {
      return this.createStreamingCompletionWatcher(onDelta);
    }

    const state = {
      text: '',
      reasoning: '',
      complete: false,
      sawCompletionResponse: false,
      activeFragment: null,
      lastError: null,
      httpStatus: 0,
      errorText: '',
      onDelta: null
    };

    const onResponse = async response => {
      const url = response.url();
      const method = response.request().method();
      if (!url.includes('chat/completion') || method !== 'POST') return;

      state.sawCompletionResponse = true;
      if (response.status() >= 400) {
        state.lastError = `DeepSeek chat/completion failed: HTTP ${response.status()}`;
        state.httpStatus = response.status();
        state.complete = true;
        try {
          state.errorText = await response.text();
        } catch {}
        return;
      }

      try {
        const body = await response.text();
        this.parseCompletionBody(body, state);
      } catch (err) {
        state.lastError = `Unable to read DeepSeek completion response: ${err.message}`;
        state.complete = true;
      }
    };

    this.page.on('response', onResponse);
    return {
      state,
      dispose: () => this.page.off('response', onResponse)
    };
  }

  createStreamingCompletionWatcher(onDelta) {
    const state = {
      text: '',
      reasoning: '',
      complete: false,
      sawCompletionResponse: false,
      activeFragment: null,
      lastError: null,
      httpStatus: 0,
      errorText: '',
      onDelta,
      buffer: ''
    };

    const requestMethods = new Map();
    const trackedRequests = new Set();

    const onRequestWillBeSent = event => {
      requestMethods.set(event.requestId, event.request?.method || '');
    };

    const onResponseReceived = async event => {
      const url = event.response?.url || '';
      const method = requestMethods.get(event.requestId);
      if (!url.includes('chat/completion') || method !== 'POST') return;

      trackedRequests.add(event.requestId);
      state.sawCompletionResponse = true;

      if ((event.response?.status || 0) >= 400) {
        state.lastError = `DeepSeek chat/completion failed: HTTP ${event.response.status}`;
        state.httpStatus = event.response.status;
        state.complete = true;
        return;
      }

      try {
        const { bufferedData } = await this.cdp.send('Network.streamResourceContent', {
          requestId: event.requestId
        });
        if (bufferedData) {
          this.consumeCompletionBytes(bufferedData, state);
        }
      } catch (err) {
        state.lastError = `Unable to stream DeepSeek completion response: ${err.message}`;
        state.complete = true;
      }
    };

    const onDataReceived = event => {
      if (!trackedRequests.has(event.requestId) || !event.data) return;
      this.consumeCompletionBytes(event.data, state);
    };

    const onLoadingFinished = event => {
      if (!trackedRequests.has(event.requestId)) return;
      this.flushCompletionBuffer(state);
      state.complete = true;
      trackedRequests.delete(event.requestId);
      requestMethods.delete(event.requestId);
    };

    const onLoadingFailed = event => {
      if (!trackedRequests.has(event.requestId)) return;
      state.lastError = event.errorText || 'DeepSeek completion network request failed';
      state.complete = true;
      trackedRequests.delete(event.requestId);
      requestMethods.delete(event.requestId);
    };

    this.cdp.on('Network.requestWillBeSent', onRequestWillBeSent);
    this.cdp.on('Network.responseReceived', onResponseReceived);
    this.cdp.on('Network.dataReceived', onDataReceived);
    this.cdp.on('Network.loadingFinished', onLoadingFinished);
    this.cdp.on('Network.loadingFailed', onLoadingFailed);

    return {
      state,
      dispose: () => {
        this.cdp?.off('Network.requestWillBeSent', onRequestWillBeSent);
        this.cdp?.off('Network.responseReceived', onResponseReceived);
        this.cdp?.off('Network.dataReceived', onDataReceived);
        this.cdp?.off('Network.loadingFinished', onLoadingFinished);
        this.cdp?.off('Network.loadingFailed', onLoadingFailed);
      }
    };
  }

  consumeCompletionBytes(base64Data, state) {
    const text = Buffer.from(base64Data, 'base64').toString('utf8');
    state.buffer += text;
    this.drainCompletionBuffer(state);
  }

  drainCompletionBuffer(state) {
    const lines = state.buffer.split(/\n/);
    state.buffer = lines.pop() || '';

    for (const line of lines) {
      this.consumeCompletionLine(line.replace(/\r$/, ''), state);
    }
  }

  flushCompletionBuffer(state) {
    if (state.buffer) {
      this.consumeCompletionLine(state.buffer.replace(/\r$/, ''), state);
      state.buffer = '';
    }
  }

  consumeCompletionLine(line, state) {
    if (!line.startsWith('data:')) return;
    const payload = line.slice(5).trim();
    if (!payload || payload === '{}' || payload === '[DONE]') return;

    let data;
    try {
      data = JSON.parse(payload);
    } catch {
      return;
    }
    this.consumePatch(data, state);
  }

  parseCompletionBody(body, state) {
    const lines = body.split(/\r?\n/);
    for (const line of lines) {
      if (!line.startsWith('data:')) continue;
      const payload = line.slice(5).trim();
      if (!payload || payload === '{}' || payload === '[DONE]') continue;

      let data;
      try {
        data = JSON.parse(payload);
      } catch {
        continue;
      }
      this.consumePatch(data, state);
    }
  }

  consumePatch(data, state) {
    if (Array.isArray(data?.v?.response?.fragments)) {
      this.consumeFragments(data.v.response.fragments, state);
    }

    if (data.p === 'response/fragments' && data.o === 'APPEND' && Array.isArray(data.v)) {
      this.consumeFragments(data.v, state);
    }

    if (data.o === 'BATCH' && data.p === 'response' && Array.isArray(data.v)) {
      for (const patch of data.v) {
        if (patch.p === 'fragments' && patch.o === 'APPEND' && Array.isArray(patch.v)) {
          this.consumeFragments(patch.v, state);
        }
        if ((patch.p === 'status' || patch.p === 'quasi_status') && patch.v === 'FINISHED') {
          state.complete = true;
        }
      }
    }

    if (data.p && typeof data.v === 'string' && /response\/fragments\/-?\d+\/content/.test(data.p)) {
      this.appendFragmentText(state, data.v);
    }

    if (typeof data.v === 'string' && !data.p && !data.o) {
      this.appendFragmentText(state, data.v);
    }

    if (data.p === 'response/status' && data.o === 'SET' && data.v === 'FINISHED') {
      state.complete = true;
    }
  }

  consumeFragments(fragments, state) {
    for (const fragment of fragments) {
      if (!fragment || typeof fragment !== 'object') continue;
      const type = String(fragment.type || '').toUpperCase();
      if (type === 'RESPONSE') {
        state.activeFragment = 'response';
        if (fragment.content) this.appendResponseText(state, fragment.content);
      } else if (type === 'THINK' || type === 'THINKING' || type === 'REASONING') {
        state.activeFragment = 'reasoning';
        if (fragment.content) this.appendReasoningText(state, fragment.content);
      } else {
        state.activeFragment = null;
      }
    }
  }

  appendFragmentText(state, value) {
    if (state.activeFragment === 'reasoning') {
      this.appendReasoningText(state, value);
    } else if (state.activeFragment === 'response') {
      this.appendResponseText(state, value);
    }
  }

  appendResponseText(state, value) {
    if (!value) return;
    state.text += value;
    state.onDelta?.({ type: 'content', value });
  }

  appendReasoningText(state, value) {
    if (!value) return;
    state.reasoning += value;
    state.onDelta?.({ type: 'reasoning', value });
  }

  async submit() {
    const btn = this.page.locator('div.ds-button--primary').first();
    try {
      await btn.waitFor({ state: 'attached', timeout: 5000 });
    } catch {
      const fallbacks = [
        'div.ds-button[class*="primary"]',
        'div[class*="ds-button"][class*="send"]',
        'button[data-testid*="send" i]',
        'button[data-testid*="submit" i]',
        '[data-testid*="send-message" i]',
        'button[aria-label*="send" i]'
      ];
      for (const sel of fallbacks) {
        const fb = this.page.locator(sel).first();
        if ((await fb.count().catch(() => 0)) > 0) {
          await this.clickSendButtonWhenReady(fb);
          return;
        }
      }
      if (await this.clickGeometricSendButton()) return;
      await this.page.keyboard.press('Enter');
      return;
    }
    await this.clickSendButtonWhenReady(btn);
  }

  async clickSendButtonWhenReady(btnLocator) {
    const started = Date.now();
    const timeout = 60000;
    const sel = 'div.ds-button--primary';

    while (Date.now() - started < timeout) {
      const state = await this.page.evaluate((s) => {
        const el = document.querySelector(s);
        if (!el) return { found: false };
        const ad = el.getAttribute('aria-disabled');
        const st = window.getComputedStyle(el);
        const disabled = ad === 'true' || st.pointerEvents === 'none' ||
          parseFloat(st.opacity) < 0.5 || /\bdisabled\b/i.test(el.className || '') ||
          st.cursor === 'wait' || st.cursor === 'not-allowed';
        return { found: true, disabled, ad, pe: st.pointerEvents, op: st.opacity, cls: (el.className || '').slice(0, 50) };
      }, sel).catch(() => ({ found: false }));

      if (!state.found) {
        await this.page.keyboard.press('Enter');
        return;
      }
      if (!state.disabled) {
        await btnLocator.click({ force: true, timeout: 3000 }).catch(async () => {
          const box = await btnLocator.boundingBox().catch(() => null);
          if (box) await this.page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
        });
        await delay(500);
        return;
      }
      await delay(300);
    }

    await btnLocator.click({ force: true }).catch(() => {});
  }

  async clickGeometricSendButton(timeoutMs = 20000) {
    const startedAt = Date.now();
    while (Date.now() - startedAt < timeoutMs) {
      const target = await this.page.evaluate(() => {
        const input = document.querySelector('textarea, [contenteditable="true"]');
        if (!input) return null;
        const inputRect = input.getBoundingClientRect();
        const inputCenterX = inputRect.left + inputRect.width / 2;
        const candidates = [...document.querySelectorAll('button, [role="button"]')]
          .map(element => {
            const rect = element.getBoundingClientRect();
            const style = window.getComputedStyle(element);
            const disabled = element.disabled || element.getAttribute('aria-disabled') === 'true';
            const visible = rect.width > 8 && rect.height > 8 && style.display !== 'none' && style.visibility !== 'hidden';
            return {
              x: rect.left,
              y: rect.top,
              width: rect.width,
              height: rect.height,
              disabled,
              visible,
              rightOfInput: rect.left > inputCenterX,
              nearInput: rect.top >= inputRect.top - 30 && rect.top <= inputRect.bottom + 90
            };
          })
          .filter(item => item.visible && item.rightOfInput && item.nearInput)
          .sort((a, b) => (b.x + b.width) - (a.x + a.width));

        return candidates[0] || null;
      }).catch(() => null);

      if (target && !target.disabled) {
        await this.page.mouse.click(target.x + target.width / 2, target.y + target.height / 2);
        return true;
      }
      await delay(300);
    }

    return false;
  }

  async waitForCompletion(watcher, startedAt) {
    try {
      while (Date.now() - startedAt < this.config.requestTimeoutMs) {
        if (watcher.state.complete) {
          if (watcher.state.lastError) {
            throw new ApiError(watcher.state.lastError, 502, 'deepseek_error', 'server_error');
          }
          return {
            text: watcher.state.text.trim(),
            reasoning: watcher.state.reasoning.trim()
          };
        }
        await delay(250);
      }

      if (!watcher.state.sawCompletionResponse) {
        throw new ApiError('Timed out waiting for DeepSeek chat/completion response', 504, 'deepseek_timeout', 'server_error');
      }
      throw new ApiError('Timed out waiting for DeepSeek to finish generating', 504, 'deepseek_timeout', 'server_error');
    } finally {
      watcher.dispose();
    }
  }
}
