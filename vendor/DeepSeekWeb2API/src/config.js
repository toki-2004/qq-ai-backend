import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const rootDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const configPath = path.resolve(process.env.CONFIG_PATH || path.join(rootDir, 'config.json'));

function loadConfigFile() {
  if (!fs.existsSync(configPath)) return {};
  try {
    return JSON.parse(fs.readFileSync(configPath, 'utf8'));
  } catch (err) {
    throw new Error(`Invalid config file ${configPath}: ${err.message}`);
  }
}

const fileConfig = loadConfigFile();

function getValue(object, pathParts, defaultValue) {
  let current = object;
  for (const part of pathParts) {
    if (!current || typeof current !== 'object' || !(part in current)) return defaultValue;
    current = current[part];
  }
  return current ?? defaultValue;
}

function boolEnv(name, defaultValue) {
  const value = process.env[name];
  if (value == null || value === '') return defaultValue;
  return ['1', 'true', 'yes', 'on'].includes(value.toLowerCase());
}

function boolConfig(envName, fileValue, defaultValue) {
  if (process.env[envName] != null && process.env[envName] !== '') {
    return boolEnv(envName, defaultValue);
  }
  if (typeof fileValue === 'boolean') return fileValue;
  if (typeof fileValue === 'string') return ['1', 'true', 'yes', 'on'].includes(fileValue.toLowerCase());
  return defaultValue;
}

function intEnv(name, fileValue, defaultValue) {
  const value = Number.parseInt(process.env[name] || '', 10);
  if (Number.isFinite(value)) return value;
  const configValue = Number.parseInt(fileValue, 10);
  return Number.isFinite(configValue) ? configValue : defaultValue;
}

function stringConfig(envName, fileValue, defaultValue = '') {
  if (process.env[envName] != null && process.env[envName] !== '') return process.env[envName];
  if (fileValue != null && fileValue !== '') return String(fileValue);
  return defaultValue;
}

function resolveProjectPath(value, defaultValue) {
  const selected = value || defaultValue;
  return path.resolve(rootDir, selected);
}

function readRegistryValue(key, valueName = '') {
  if (process.platform !== 'win32') return '';
  try {
    const args = valueName ? ['query', key, '/v', valueName] : ['query', key, '/ve'];
    const output = execFileSync('reg', args, {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
      windowsHide: true
    });
    for (const line of output.split(/\r?\n/)) {
      const match = line.match(/\sREG_\w+\s+(.+)$/);
      if (match) return match[1].trim();
    }
  } catch {
    return '';
  }
  return '';
}

function executableFromCommand(command) {
  const value = String(command || '').trim();
  const quoted = value.match(/^"([^"]+?\.exe)"/i);
  if (quoted) return path.normalize(quoted[1]);

  const plain = value.match(/^([^"\s]+?\.exe)(?:\s|$)/i);
  return plain ? path.normalize(plain[1]) : '';
}

function browserNameFromPath(executablePath, prefix = '') {
  const base = path.basename(executablePath || '').toLowerCase();
  const names = {
    'chrome.exe': 'chrome',
    'msedge.exe': 'edge',
    'brave.exe': 'brave',
    'vivaldi.exe': 'vivaldi',
    'opera.exe': 'opera',
    'opera_gx.exe': 'opera-gx',
    'chromium.exe': 'chromium'
  };
  const name = names[base] || '';
  return name && prefix ? `${prefix}-${name}` : name;
}

function isChromiumBrowser(executablePath) {
  return Boolean(browserNameFromPath(executablePath));
}

function findDefaultBrowser() {
  if (process.platform !== 'win32') return { name: '', executablePath: '' };

  const userChoiceKeys = [
    'HKCU\\Software\\Microsoft\\Windows\\Shell\\Associations\\UrlAssociations\\https\\UserChoice',
    'HKCU\\Software\\Microsoft\\Windows\\Shell\\Associations\\UrlAssociations\\http\\UserChoice'
  ];

  for (const userChoiceKey of userChoiceKeys) {
    const progId = readRegistryValue(userChoiceKey, 'ProgId');
    if (!progId) continue;

    const commandKeys = [
      `HKCU\\Software\\Classes\\${progId}\\shell\\open\\command`,
      `HKCR\\${progId}\\shell\\open\\command`,
      `HKLM\\Software\\Classes\\${progId}\\shell\\open\\command`
    ];

    for (const commandKey of commandKeys) {
      const executablePath = executableFromCommand(readRegistryValue(commandKey));
      if (executablePath && fs.existsSync(executablePath) && isChromiumBrowser(executablePath)) {
        return { name: browserNameFromPath(executablePath, 'default'), executablePath };
      }
    }
  }

  return { name: '', executablePath: '' };
}

function findInstalledBrowser(preferBrowser) {
  if (process.platform !== 'win32') return { name: '', executablePath: '' };

  const programFiles = process.env.PROGRAMFILES || 'C:\\Program Files';
  const programFilesX86 = process.env['PROGRAMFILES(X86)'] || 'C:\\Program Files (x86)';
  const localAppData = process.env.LOCALAPPDATA || '';
  const candidates = {
    edge: [
      path.join(programFilesX86, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
      path.join(programFiles, 'Microsoft', 'Edge', 'Application', 'msedge.exe')
    ],
    chrome: [
      path.join(programFiles, 'Google', 'Chrome', 'Application', 'chrome.exe'),
      path.join(programFilesX86, 'Google', 'Chrome', 'Application', 'chrome.exe'),
      localAppData ? path.join(localAppData, 'Google', 'Chrome', 'Application', 'chrome.exe') : ''
    ]
  };

  const order = preferBrowser === 'edge' ? ['edge', 'chrome'] : ['chrome'];
  for (const name of order) {
    for (const executablePath of candidates[name]) {
      if (executablePath && fs.existsSync(executablePath)) {
        return { name, executablePath };
      }
    }
  }

  return { name: '', executablePath: '' };
}

function findBrowser(preferBrowser) {
  const preference = String(preferBrowser || 'default').toLowerCase();
  if (preference === 'default') {
    const defaultBrowser = findDefaultBrowser();
    if (defaultBrowser.executablePath) return defaultBrowser;
    return findInstalledBrowser('chrome');
  }
  return findInstalledBrowser(preference);
}

const browserFileConfig = getValue(fileConfig, ['browser'], {});
const configuredBrowserPath = stringConfig('BROWSER_PATH', browserFileConfig.executablePath, '');
const configuredBrowserChannel = stringConfig('BROWSER_CHANNEL', browserFileConfig.channel, 'auto');
const configuredBrowserPrefer = stringConfig('BROWSER_PREFER', browserFileConfig.prefer, 'default');
const detectedBrowser = configuredBrowserPath || configuredBrowserChannel !== 'auto'
  ? { name: '', executablePath: '' }
  : findBrowser(configuredBrowserPrefer);

export const config = {
  rootDir,
  configPath,
  port: intEnv('PORT', getValue(fileConfig, ['server', 'port'], undefined), 3000),
  host: stringConfig('HOST', getValue(fileConfig, ['server', 'host'], undefined), '127.0.0.1'),
  apiKey: stringConfig('API_KEY', getValue(fileConfig, ['server', 'apiKey'], undefined), ''),
  publicBaseUrl: stringConfig('PUBLIC_BASE_URL', getValue(fileConfig, ['server', 'publicBaseUrl'], undefined), ''),
  targetUrl: stringConfig('DEEPSEEK_URL', getValue(fileConfig, ['deepseek', 'url'], undefined), 'https://chat.deepseek.com/'),
  userDataDir: path.resolve(process.env.USER_DATA_DIR || resolveProjectPath(getValue(fileConfig, ['paths', 'userDataDir'], undefined), 'data/user-data')),
  tempDir: path.resolve(process.env.TEMP_DIR || resolveProjectPath(getValue(fileConfig, ['paths', 'tempDir'], undefined), 'tmp')),
  headless: boolConfig('HEADLESS', browserFileConfig.headless, false),
  // 闲置多久关掉无头浏览器（毫秒，0=不关）：一套 Chromium 8 进程几百 MB，
  // 桌宠大多时间用不到；下次请求按需重开并回到自己的那段对话
  browserIdleMs: intEnv('BROWSER_IDLE_MS', browserFileConfig.idleTimeoutMs, 1200000),
  loginMode: boolEnv('DEEPSEEK_LOGIN', false) || process.argv.includes('--login'),
  // 5 分钟：网页版读网页/长思考经常超过 1-3 分钟，太早收手会误报失败
  requestTimeoutMs: intEnv('REQUEST_TIMEOUT_MS', getValue(fileConfig, ['limits', 'requestTimeoutMs'], undefined), 300000),
  uploadTimeoutMs: intEnv('UPLOAD_TIMEOUT_MS', getValue(fileConfig, ['limits', 'uploadTimeoutMs'], undefined), 90000),
  keepaliveMs: intEnv('KEEPALIVE_MS', getValue(fileConfig, ['limits', 'keepaliveMs'], undefined), 3000),
  imageLimit: intEnv('IMAGE_LIMIT', getValue(fileConfig, ['limits', 'imageLimit'], undefined), 8),
  maxBodyBytes: intEnv('MAX_BODY_BYTES', getValue(fileConfig, ['limits', 'maxBodyBytes'], undefined), 50 * 1024 * 1024),
  newConversationAfter: intEnv('NEW_CONVERSATION_AFTER', getValue(fileConfig, ['conversation', 'maxMessages'], undefined), 20),
  browserChannel: configuredBrowserChannel === 'auto' ? '' : configuredBrowserChannel,
  browserExecutablePath: configuredBrowserPath ? path.resolve(rootDir, configuredBrowserPath) : detectedBrowser.executablePath,
  browserName: configuredBrowserPath ? 'custom' : (configuredBrowserChannel !== 'auto' ? configuredBrowserChannel : detectedBrowser.name),
  models: Array.isArray(getValue(fileConfig, ['models'], [])) ? getValue(fileConfig, ['models'], []) : []
};
