import { config } from './config.js';

const defaultModels = [
  {
    id: 'deepseek',
    aliases: ['deepseek-chat'],
    owned_by: 'deepseek-web',
    image_policy: 'optional',
    capabilities: { thinking: false, search: false, expert: false, vision: false }
  },
  {
    id: 'deepseek-thinking',
    aliases: ['deepseek-reasoner'],
    owned_by: 'deepseek-web',
    image_policy: 'optional',
    capabilities: { thinking: true, search: false, expert: false, vision: false }
  },
  {
    id: 'deepseek-search',
    aliases: [],
    owned_by: 'deepseek-web',
    image_policy: 'optional',
    capabilities: { thinking: false, search: true, expert: false, vision: false }
  },
  {
    id: 'deepseek-expert',
    aliases: [],
    owned_by: 'deepseek-web',
    image_policy: 'optional',
    capabilities: { thinking: false, search: false, expert: true, vision: false }
  },
  {
    id: 'deepseek-vision',
    aliases: ['deepseek-image', 'deepseek-vl'],
    owned_by: 'deepseek-web',
    image_policy: 'optional',
    capabilities: { thinking: false, search: false, expert: false, vision: true }
  },
  {
    id: 'deepseek-vision-thinking',
    aliases: [],
    owned_by: 'deepseek-web',
    image_policy: 'optional',
    capabilities: { thinking: true, search: false, expert: false, vision: true }
  }
];

function normalizeModel(model) {
  return {
    id: String(model.id || '').trim(),
    aliases: Array.isArray(model.aliases) ? model.aliases.map(String) : [],
    owned_by: model.owned_by || model.ownedBy || 'deepseek-web',
    image_policy: model.image_policy || model.imagePolicy || 'optional',
    capabilities: {
      thinking: Boolean(model.capabilities?.thinking),
      search: Boolean(model.capabilities?.search),
      expert: Boolean(model.capabilities?.expert),
      vision: Boolean(model.capabilities?.vision)
    }
  };
}

export const models = (config.models.length > 0 ? config.models : defaultModels)
  .map(normalizeModel)
  .filter(model => model.id);

export function resolveModel(modelId) {
  if (!modelId) return models[0];
  return models.find(model => model.id === modelId || model.aliases.includes(modelId)) || null;
}

export function listModels() {
  const created = Math.floor(Date.now() / 1000);
  return {
    object: 'list',
    data: models.map(model => ({
      id: model.id,
      object: 'model',
      created,
      owned_by: model.owned_by,
      image_policy: model.image_policy
    }))
  };
}
