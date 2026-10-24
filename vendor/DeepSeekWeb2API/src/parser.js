import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { ApiError } from './http.js';

const IMAGE_EXTENSIONS = {
  'image/jpeg': '.jpg',
  'image/jpg': '.jpg',
  'image/png': '.png',
  'image/webp': '.webp',
  'image/gif': '.gif'
};

export async function parseChatRequest(body, options) {
  if (!Array.isArray(body.messages) || body.messages.length === 0) {
    throw new ApiError('messages is required', 400, 'invalid_request_error', 'invalid_request_error');
  }

  const imagePaths = [];
  let imageIndex = 0;

  async function parseContent(content) {
    if (typeof content === 'string') return content;
    if (!Array.isArray(content)) return '';

    const parts = [];
    for (const item of content) {
      if (!item || typeof item !== 'object') continue;
      if (item.type === 'text') {
        parts.push(item.text || '');
        continue;
      }
      if (item.type !== 'image_url' || !item.image_url?.url) continue;

      imageIndex += 1;
      if (options.imageLimit > 0 && imageIndex > options.imageLimit) {
        throw new ApiError(`Too many images. Max: ${options.imageLimit}`, 400, 'too_many_images', 'invalid_request_error');
      }

      const imagePath = await materializeImage(item.image_url.url, options.tempDir);
      imagePaths.push(imagePath);
      parts.push(`[Image ${imageIndex}]`);
    }

    return parts.filter(Boolean).join('\n');
  }

  const system = [];
  const history = [];
  let lastUserIndex = -1;

  for (let i = body.messages.length - 1; i >= 0; i -= 1) {
    if (body.messages[i]?.role === 'user') {
      lastUserIndex = i;
      break;
    }
  }

  if (lastUserIndex === -1) {
    throw new ApiError('At least one user message is required', 400, 'invalid_request_error', 'invalid_request_error');
  }

  for (let i = 0; i < body.messages.length; i += 1) {
    const message = body.messages[i];
    if (message.role === 'system') {
      const text = (await parseContent(message.content)).trim();
      if (!text) continue;
      system.push(text);
    } else if (i < lastUserIndex) {
      const text = (await parseContent(message.content)).trim();
      if (!text) continue;
      const role = message.role === 'assistant' ? 'Assistant' : 'User';
      history.push(`${role}: ${text}`);
    }
  }

  const currentText = (await parseContent(body.messages[lastUserIndex].content)).trim();
  const hasContext = system.length > 0 || history.length > 0;
  const promptParts = [];

  if (system.length > 0) {
    promptParts.push(`System instructions:\n${system.join('\n\n')}`);
  }
  if (history.length > 0) {
    promptParts.push(`Conversation history:\n${history.join('\n')}`);
  }
  promptParts.push(hasContext ? `Current user message:\n${currentText}` : currentText);

  const prompt = promptParts.join('\n\n').trim();
  if (!prompt && imagePaths.length === 0) {
    throw new ApiError('The last user message is empty', 400, 'invalid_request_error', 'invalid_request_error');
  }

  return {
    prompt,
    imagePaths,
    stream: body.stream === true,
    requestedModel: body.model || 'deepseek'
  };
}

export async function cleanupFiles(paths) {
  await Promise.allSettled(paths.map(filePath => fs.unlink(filePath)));
}

async function materializeImage(url, tempDir) {
  await fs.mkdir(tempDir, { recursive: true });

  if (url.startsWith('data:image/')) {
    return saveDataUrl(url, tempDir);
  }

  if (url.startsWith('http://') || url.startsWith('https://')) {
    return downloadImage(url, tempDir);
  }

  throw new ApiError('Only data:image and http(s) image URLs are supported', 400, 'invalid_image_url', 'invalid_request_error');
}

async function saveDataUrl(dataUrl, tempDir) {
  const match = dataUrl.match(/^data:(image\/[a-zA-Z0-9.+-]+);base64,(.*)$/);
  if (!match) {
    throw new ApiError('Invalid data:image URL', 400, 'invalid_image_url', 'invalid_request_error');
  }

  const mime = match[1].toLowerCase();
  const ext = IMAGE_EXTENSIONS[mime] || '.img';
  const buffer = Buffer.from(match[2], 'base64');
  if (buffer.length === 0) {
    throw new ApiError('Image payload is empty', 400, 'invalid_image_url', 'invalid_request_error');
  }

  const filePath = path.join(tempDir, `img_${Date.now()}_${crypto.randomUUID()}${ext}`);
  await fs.writeFile(filePath, buffer);
  return filePath;
}

async function downloadImage(url, tempDir) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30000);
  let response;
  try {
    response = await fetch(url, { signal: controller.signal });
  } finally {
    clearTimeout(timeout);
  }

  if (!response.ok) {
    throw new ApiError(`Image download failed: HTTP ${response.status}`, 400, 'invalid_image_url', 'invalid_request_error');
  }

  const contentType = (response.headers.get('content-type') || '').split(';')[0].toLowerCase();
  if (!contentType.startsWith('image/')) {
    throw new ApiError(`URL is not an image: ${contentType || 'unknown content-type'}`, 400, 'invalid_image_url', 'invalid_request_error');
  }

  const buffer = Buffer.from(await response.arrayBuffer());
  if (buffer.length === 0) {
    throw new ApiError('Downloaded image is empty', 400, 'invalid_image_url', 'invalid_request_error');
  }

  const ext = IMAGE_EXTENSIONS[contentType] || extensionFromUrl(url) || '.img';
  const filePath = path.join(tempDir, `img_${Date.now()}_${crypto.randomUUID()}${ext}`);
  await fs.writeFile(filePath, buffer);
  return filePath;
}

function extensionFromUrl(url) {
  try {
    const ext = path.extname(new URL(url).pathname).toLowerCase();
    return ['.jpg', '.jpeg', '.png', '.webp', '.gif'].includes(ext) ? ext : '';
  } catch {
    return '';
  }
}
