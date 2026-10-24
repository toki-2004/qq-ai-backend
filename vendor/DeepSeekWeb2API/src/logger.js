const levels = { debug: 10, info: 20, warn: 30, error: 40 };
const currentLevel = levels[process.env.LOG_LEVEL || 'info'] || levels.info;

function write(level, message, meta) {
  if (levels[level] < currentLevel) return;
  const line = {
    time: new Date().toISOString(),
    level,
    message,
    ...(meta ? { meta } : {})
  };
  process.stderr.write(`${JSON.stringify(line)}\n`);
}

export const logger = {
  debug: (message, meta) => write('debug', message, meta),
  info: (message, meta) => write('info', message, meta),
  warn: (message, meta) => write('warn', message, meta),
  error: (message, meta) => write('error', message, meta)
};
