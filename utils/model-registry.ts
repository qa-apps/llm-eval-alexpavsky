/**
 * model-registry.ts
 *
 * Dynamic LLM model registry for the judge framework.
 * Mirrors the model-selection logic from alexpavsky/chat_server.py:
 *   - Static fallback list (Groq + OpenRouter + HuggingFace)
 *   - Live fetch from OpenRouter /api/v1/models (free models only)
 *   - Live fetch from HuggingFace /api/models (top text-generation)
 *   - Live fetch from Groq /openai/v1/models
 *   - Models sorted by `created` timestamp → newest first
 *   - Disk cache: refreshed every 7 days (matches the server's sync loop)
 *   - Tier classification: S (small/fast) · M (medium) · H (heavy/best)
 */

import * as fs from 'fs';
import * as path from 'path';
import * as https from 'https';

// ─── Types ────────────────────────────────────────────────────────────────────

export type ModelTier = 'S' | 'M' | 'H';
export type ProviderName = 'ollama' | 'deepseek' | 'groq' | 'openrouter' | 'huggingface' | 'gemini' | 'cerebras' | 'sambanova' | 'mistral';

export interface ModelEntry {
  id: string;
  label: string;
  provider: ProviderName;
  tier: ModelTier;
  free?: boolean;
  vision?: boolean;
  coding?: boolean;
  reasoning?: boolean;
  created?: number; // unix timestamp — used for "newest first" sort
}

export interface ResolvedProvider {
  baseUrl: string;
  model: string;
  headers: Record<string, string>;
  providerName: ProviderName;
}

function resolveLocalProvider(): ResolvedProvider | null {
  if (process.env.ALLOW_LOCAL_QA !== '1') return null;
  const configuredBaseUrl = process.env.LOCAL_LLM_BASE_URL || process.env.OLLAMA_BASE_URL;
  if (!configuredBaseUrl) return null;

  const apiRoot = configuredBaseUrl.replace(/\/$/, '');
  const endpoint = new URL(apiRoot);
  if (!['127.0.0.1', 'localhost', '::1'].includes(endpoint.hostname)) return null;
  const model = process.env.LOCAL_LLM_MODEL || 'qwen3.8:27b';
  if (model.toLowerCase().includes('gpt-oss')) return null;
  const baseUrl = apiRoot.endsWith('/v1')
    ? `${apiRoot}/chat/completions`
    : `${apiRoot}/v1/chat/completions`;
  const apiKey = process.env.LOCAL_LLM_API_KEY || process.env.OLLAMA_API_KEY || 'ollama';

  return {
    baseUrl,
    model,
    headers: {
      Authorization: `Bearer ${apiKey}`,
      'Content-Type': 'application/json',
    },
    providerName: 'ollama',
  };
}

function resolveDeepSeekProvider(): ResolvedProvider | null {
  const key = process.env.DEEPSEEK_API_KEY;
  if (!key) return null;
  return {
    baseUrl: 'https://api.deepseek.com/v1/chat/completions',
    model: process.env.DEEPSEEK_MODEL || 'deepseek-v4-pro',
    headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' },
    providerName: 'deepseek',
  };
}

// ─── Cache ────────────────────────────────────────────────────────────────────

const CACHE_FILE = path.join(process.cwd(), '.model-registry-cache.json');
const CACHE_TTL_MS = 7 * 24 * 60 * 60 * 1000; // 7 days — same as chat_server.py

interface RegistryCache {
  fetchedAt: number;
  models: ModelEntry[];
}

function readCache(): RegistryCache | null {
  try {
    if (!fs.existsSync(CACHE_FILE)) return null;
    const data = JSON.parse(fs.readFileSync(CACHE_FILE, 'utf-8')) as RegistryCache;
    if (Date.now() - data.fetchedAt > CACHE_TTL_MS) return null;
    return data;
  } catch {
    return null;
  }
}

function writeCache(models: ModelEntry[]): void {
  try {
    const cache: RegistryCache = { fetchedAt: Date.now(), models };
    fs.writeFileSync(CACHE_FILE, JSON.stringify(cache, null, 2), 'utf-8');
  } catch (e) {
    console.warn('[model-registry] Failed to write cache:', e);
  }
}

// ─── Static fallback models (mirrors _STATIC_MODELS in chat_server.py) ────────

const STATIC_MODELS: ModelEntry[] = [
  // Groq
  { id: 'llama-3.1-8b-instant',                          label: 'Llama 3.1 8B Instant',     provider: 'groq', tier: 'S' },
  { id: 'llama-3.3-70b-versatile',                       label: 'Llama 3.3 70B Versatile',  provider: 'groq', tier: 'M' },
  { id: 'meta-llama/llama-4-scout-17b-16e-instruct',     label: 'Llama 4 Scout 17B',        provider: 'groq', tier: 'M' },
  { id: 'meta-llama/llama-4-maverick-17b-128e-instruct', label: 'Llama 4 Maverick 17B',     provider: 'groq', tier: 'M' },
  { id: 'qwen/qwen3-32b',                                label: 'Qwen 3 32B',               provider: 'groq', tier: 'H', coding: true },
  { id: 'moonshotai/kimi-k2-instruct',                   label: 'Kimi K2 Instruct',         provider: 'groq', tier: 'H' },
  // OpenRouter (free)
  { id: 'google/gemma-3-27b-it:free',                    label: 'Gemma 3 27B',              provider: 'openrouter', free: true, tier: 'S' },
  { id: 'mistralai/mistral-small-3.1-24b-instruct:free', label: 'Mistral Small 24B',        provider: 'openrouter', free: true, tier: 'S' },
  { id: 'meta-llama/llama-3.3-70b-instruct:free',        label: 'Llama 3.3 70B',            provider: 'openrouter', free: true, tier: 'M' },
  { id: 'deepseek/deepseek-r1-0528:free',                label: 'DeepSeek R1',              provider: 'openrouter', free: true, tier: 'H', reasoning: true },
  { id: 'qwen/qwen3-coder:free',                         label: 'Qwen 3 Coder 480B',        provider: 'openrouter', free: true, tier: 'H', coding: true },
  { id: 'nousresearch/hermes-3-llama-3.1-405b:free',     label: 'Hermes 3 405B',            provider: 'openrouter', free: true, tier: 'H' },
  // Cerebras (free, ultra-fast LPU inference)
  { id: 'llama-3.3-70b',                                 label: 'Llama 3.3 70B (Cerebras)', provider: 'cerebras',   free: true, tier: 'M' },
  { id: 'llama-4-scout-17b-16e-instruct',                label: 'Llama 4 Scout (Cerebras)', provider: 'cerebras',   free: true, tier: 'M' },
  { id: 'qwen-3-32b',                                    label: 'Qwen 3 32B (Cerebras)',    provider: 'cerebras',   free: true, tier: 'M', coding: true },
  // SambaNova (free, RDU inference)
  { id: 'Meta-Llama-3.3-70B-Instruct',                   label: 'Llama 3.3 70B (SN)',       provider: 'sambanova',  free: true, tier: 'M' },
  { id: 'DeepSeek-R1',                                   label: 'DeepSeek R1 (SN)',         provider: 'sambanova',  free: true, tier: 'H', reasoning: true },
  { id: 'Qwen2.5-72B-Instruct',                          label: 'Qwen 2.5 72B (SN)',        provider: 'sambanova',  free: true, tier: 'M' },
  // Mistral (free tier)
  { id: 'mistral-small-latest',                          label: 'Mistral Small Latest',     provider: 'mistral',    free: true, tier: 'S' },
  { id: 'open-mistral-nemo',                             label: 'Mistral Nemo',             provider: 'mistral',    free: true, tier: 'S' },
  { id: 'mistral-medium-latest',                         label: 'Mistral Medium Latest',    provider: 'mistral',    free: true, tier: 'M' },
];

// ─── HTTP helper ──────────────────────────────────────────────────────────────

function fetchJson<T>(url: string, headers: Record<string, string> = {}): Promise<T> {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { headers: { 'User-Agent': 'PW_alexpavsky/1.0', ...headers } }, (res) => {
      let data = '';
      res.on('data', (chunk) => (data += chunk));
      res.on('end', () => {
        try { resolve(JSON.parse(data)); }
        catch (e) { reject(new Error(`JSON parse error from ${url}: ${e}`)); }
      });
    });
    req.on('error', reject);
    req.setTimeout(15000, () => { req.destroy(); reject(new Error(`Timeout fetching ${url}`)); });
  });
}

// ─── Tier classifier (mirrors chat_server.py logic) ──────────────────────────

function classifyTier(id: string, contextLength = 0): ModelTier {
  const lower = id.toLowerCase();
  if (contextLength >= 64000 || /70b|72b|120b|405b|r1/.test(lower)) return 'H';
  if (contextLength >= 16000 || /27b|32b|34b|14b/.test(lower)) return 'M';
  return 'S';
}

function annotateModel(id: string, name: string): Pick<ModelEntry, 'vision' | 'coding' | 'reasoning'> {
  const lower = id.toLowerCase();
  return {
    vision:    /vision/.test(lower) ? true : undefined,
    coding:    /coder|code/.test(lower) ? true : undefined,
    reasoning: /r1|reason|think/.test(lower) ? true : undefined,
  };
}

// ─── Dynamic fetchers ─────────────────────────────────────────────────────────

async function fetchOpenRouterModels(): Promise<ModelEntry[]> {
  try {
    const data = await fetchJson<{ data: any[] }>('https://openrouter.ai/api/v1/models');
    const models: ModelEntry[] = [];
    for (const m of data.data || []) {
      const pricing = m.pricing || {};
      if (pricing.prompt !== '0' || pricing.completion !== '0') continue;
      const lower = (m.id as string).toLowerCase();
      if (!lower.endsWith(':free')) continue;
      if (/test|experimental/.test(lower)) continue;
      models.push({
        id:      m.id,
        label:   m.name || m.id,
        provider: 'openrouter',
        free:    true,
        created: parseInt(m.created || '0', 10),
        tier:    classifyTier(m.id, parseInt(m.context_length || '0', 10)),
        ...annotateModel(m.id, m.name || ''),
      });
    }
    console.log(`[model-registry] OpenRouter: fetched ${models.length} free models`);
    return models;
  } catch (e) {
    console.warn('[model-registry] OpenRouter fetch failed:', e);
    return [];
  }
}

async function fetchHuggingFaceModels(): Promise<ModelEntry[]> {
  try {
    const data = await fetchJson<any[]>(
      'https://huggingface.co/api/models?pipeline_tag=text-generation&sort=downloads&direction=-1&limit=30',
    );
    const models: ModelEntry[] = [];
    for (const m of data) {
      const id: string = m.id || '';
      const lower = id.toLowerCase();
      if (/test|experimental|gpt2|\\bopt\\b/.test(lower)) continue;
      let created = 0;
      if (m.createdAt) {
        try { created = Math.floor(new Date(m.createdAt).getTime() / 1000); } catch {}
      }
      models.push({
        id,
        label:   `HF ${id.split('/').pop()}`,
        provider: 'huggingface',
        free:    true,
        created,
        tier:    classifyTier(id),
        ...annotateModel(id, ''),
      });
    }
    console.log(`[model-registry] HuggingFace: fetched ${models.length} models`);
    return models;
  } catch (e) {
    console.warn('[model-registry] HuggingFace fetch failed:', e);
    return [];
  }
}

async function fetchGroqModels(): Promise<ModelEntry[]> {
  const key = process.env.GROQ_API_KEY;
  if (!key) return [];
  try {
    const data = await fetchJson<{ data: any[] }>(
      'https://api.groq.com/openai/v1/models',
      { Authorization: `Bearer ${key}` },
    );
    const models: ModelEntry[] = [];
    for (const m of data.data || []) {
      const id: string = m.id || '';
      if (/whisper|tts|guard|vision/.test(id.toLowerCase())) continue; // skip non-chat models
      models.push({
        id,
        label:   m.id,
        provider: 'groq',
        free:    false,
        created: parseInt(m.created || '0', 10),
        tier:    classifyTier(id),
        ...annotateModel(id, ''),
      });
    }
    console.log(`[model-registry] Groq: fetched ${models.length} models`);
    return models;
  } catch (e) {
    console.warn('[model-registry] Groq fetch failed:', e);
    return [];
  }
}

async function fetchCerebrasModels(): Promise<ModelEntry[]> {
  const key = process.env.CEREBRAS_API_KEY;
  if (!key) return [];
  try {
    const data = await fetchJson<{ models: any[] }>(
      'https://api.cerebras.ai/v1/models',
      { Authorization: `Bearer ${key}` },
    );
    const models: ModelEntry[] = [];
    for (const m of data.models || []) {
      const id: string = m.id || '';
      if (!id || /whisper|embed/.test(id.toLowerCase())) continue;
      models.push({
        id,
        label:   `${id} (Cerebras)`,
        provider: 'cerebras',
        free:    true,
        created: parseInt(m.created || '0', 10),
        tier:    classifyTier(id),
        ...annotateModel(id, ''),
      });
    }
    console.log(`[model-registry] Cerebras: fetched ${models.length} models`);
    return models;
  } catch (e) {
    console.warn('[model-registry] Cerebras fetch failed:', e);
    return [];
  }
}

async function fetchSambanovaModels(): Promise<ModelEntry[]> {
  const key = process.env.SAMBANOVA_API_KEY;
  if (!key) return [];
  try {
    const data = await fetchJson<{ data: any[] }>(
      'https://api.sambanova.ai/v1/models',
      { Authorization: `Bearer ${key}` },
    );
    const models: ModelEntry[] = [];
    for (const m of (data.data || [])) {
      const id: string = m.id || '';
      if (!id || /embed|vision/.test(id.toLowerCase())) continue;
      models.push({
        id,
        label:   `${id} (SambaNova)`,
        provider: 'sambanova',
        free:    true,
        created: parseInt(m.created || '0', 10),
        tier:    classifyTier(id),
        ...annotateModel(id, ''),
      });
    }
    console.log(`[model-registry] SambaNova: fetched ${models.length} models`);
    return models;
  } catch (e) {
    console.warn('[model-registry] SambaNova fetch failed:', e);
    return [];
  }
}

async function fetchMistralModels(): Promise<ModelEntry[]> {
  const key = process.env.MISTRAL_API_KEY;
  if (!key) return [];
  try {
    const data = await fetchJson<{ data: any[] }>(
      'https://api.mistral.ai/v1/models',
      { Authorization: `Bearer ${key}` },
    );
    const models: ModelEntry[] = [];
    for (const m of (data.data || [])) {
      const id: string = m.id || '';
      if (!id || /embed|moderation/.test(id.toLowerCase())) continue;
      models.push({
        id,
        label:   m.name || id,
        provider: 'mistral',
        free:    true,
        created: typeof m.created === 'number' ? m.created : 0,
        tier:    classifyTier(id),
        ...annotateModel(id, ''),
      });
    }
    console.log(`[model-registry] Mistral: fetched ${models.length} models`);
    return models;
  } catch (e) {
    console.warn('[model-registry] Mistral fetch failed:', e);
    return [];
  }
}

// ## Main registry function

let _inMemoryModels: ModelEntry[] | null = null;

/**
 * Returns the full model list, sorted by `created` descending (newest first).
 * Uses disk cache (7-day TTL) then falls back to static list.
 */
export async function getModelRegistry(forceRefresh = false): Promise<ModelEntry[]> {
  if (_inMemoryModels && !forceRefresh) return _inMemoryModels;

  // Try disk cache first
  if (!forceRefresh) {
    const cached = readCache();
    if (cached) {
      _inMemoryModels = cached.models.filter((m) => !m.id.toLowerCase().includes('gpt-oss'));
      return _inMemoryModels;
    }
  }

  // Fetch dynamically from all providers in parallel
  const [orModels, hfModels, groqModels, cerebrasModels, snModels, mistralModels] = await Promise.all([
    fetchOpenRouterModels(),
    fetchHuggingFaceModels(),
    fetchGroqModels(),
    fetchCerebrasModels(),
    fetchSambanovaModels(),
    fetchMistralModels(),
  ]);

  // Merge: static first, then dynamic (dedup by id)
  const merged = [...STATIC_MODELS];
  const seenIds = new Set(merged.map((m) => m.id));
  for (const m of [...orModels, ...hfModels, ...groqModels, ...cerebrasModels, ...snModels, ...mistralModels]) {
    if (m.id.toLowerCase().includes('gpt-oss')) continue;
    if (!seenIds.has(m.id)) {
      merged.push(m);
      seenIds.add(m.id);
    }
  }

  // Sort newest first (mirrors _update_global_models in chat_server.py)
  merged.sort((a, b) => (b.created ?? 0) - (a.created ?? 0));

  writeCache(merged);
  _inMemoryModels = merged;
  console.log(`[model-registry] Registry ready: ${merged.length} total models`);
  return merged;
}

// ─── Provider resolution ──────────────────────────────────────────────────────

/**
 * Returns the best available judge provider with the newest capable model.
 * Priority: Groq → OpenRouter → HuggingFace → Gemini
 * Within each provider, prefers Tier H → M → S, then newest by created.
 */
export async function resolveBestProvider(
  preferTier: ModelTier = 'M',
): Promise<ResolvedProvider> {
  const deepseek = resolveDeepSeekProvider();
  if (deepseek) return deepseek;
  const local = resolveLocalProvider();
  if (local) return local;

  const models = await getModelRegistry();

  const tierOrder: ModelTier[] = preferTier === 'H'
    ? ['H', 'M', 'S']
    : preferTier === 'M'
      ? ['M', 'H', 'S']
      : ['S', 'M', 'H'];

  const pickBest = (provider: ProviderName): ModelEntry | undefined => {
    const available = models.filter((m) => m.provider === provider);
    for (const tier of tierOrder) {
      const match = available.find((m) => m.tier === tier);
      if (match) return match;
    }
    return available[0];
  };

  // Groq (fastest, first priority)
  if (process.env.GROQ_API_KEY) {
    const m = pickBest('groq');
    if (m) {
      return {
        baseUrl: 'https://api.groq.com/openai/v1/chat/completions',
        model: m.id,
        headers: {
          Authorization: `Bearer ${process.env.GROQ_API_KEY}`,
          'Content-Type': 'application/json',
        },
        providerName: 'groq',
      };
    }
  }

  // Cerebras (ultra-fast LPU, free)
  if (process.env.CEREBRAS_API_KEY) {
    const m = pickBest('cerebras');
    if (m) {
      return {
        baseUrl: 'https://api.cerebras.ai/v1/chat/completions',
        model: m.id,
        headers: {
          Authorization: `Bearer ${process.env.CEREBRAS_API_KEY}`,
          'Content-Type': 'application/json',
        },
        providerName: 'cerebras',
      };
    }
  }

  // SambaNova (RDU inference, free)
  if (process.env.SAMBANOVA_API_KEY) {
    const m = pickBest('sambanova');
    if (m) {
      return {
        baseUrl: 'https://api.sambanova.ai/v1/chat/completions',
        model: m.id,
        headers: {
          Authorization: `Bearer ${process.env.SAMBANOVA_API_KEY}`,
          'Content-Type': 'application/json',
        },
        providerName: 'sambanova',
      };
    }
  }

  // Mistral (free tier)
  if (process.env.MISTRAL_API_KEY) {
    const m = pickBest('mistral');
    if (m) {
      return {
        baseUrl: 'https://api.mistral.ai/v1/chat/completions',
        model: m.id,
        headers: {
          Authorization: `Bearer ${process.env.MISTRAL_API_KEY}`,
          'Content-Type': 'application/json',
        },
        providerName: 'mistral',
      };
    }
  }

  // OpenRouter
  if (process.env.OPENROUTER_API_KEY) {
    const m = pickBest('openrouter');
    if (m) {
      return {
        baseUrl: 'https://openrouter.ai/api/v1/chat/completions',
        model: m.id,
        headers: {
          Authorization: `Bearer ${process.env.OPENROUTER_API_KEY}`,
          'Content-Type': 'application/json',
          'HTTP-Referer': 'https://github.com/PW_alexpavsky',
          'X-Title': 'PW AlexPavsky CI',
        },
        providerName: 'openrouter',
      };
    }
  }

  // HuggingFace (via Inference API)
  if (process.env.HF_TOKEN) {
    const m = pickBest('huggingface');
    if (m) {
      return {
        baseUrl: `https://api-inference.huggingface.co/models/${m.id}/v1/chat/completions`,
        model: m.id,
        headers: {
          Authorization: `Bearer ${process.env.HF_TOKEN}`,
          'Content-Type': 'application/json',
        },
        providerName: 'huggingface',
      };
    }
  }

  // Gemini (OpenAI-compatible endpoint)
  if (process.env.GEMINI_API_KEY) {
    return {
      baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions',
      model: 'gemini-3.5-flash',
      headers: {
        Authorization: `Bearer ${process.env.GEMINI_API_KEY}`,
        'Content-Type': 'application/json',
      },
      providerName: 'gemini',
    };
  }

  // No provider configured
  return { baseUrl: '', model: '', headers: { Authorization: '', 'Content-Type': 'application/json' }, providerName: 'groq' };
}

/**
 * Returns ALL configured providers in priority order (Groq first, then fallbacks).
 * Used by the judge retry loop to skip failed / rate-limited providers.
 */
export async function resolveAllProviders(
  preferTier: ModelTier = 'M',
): Promise<ResolvedProvider[]> {
  const deepseek = resolveDeepSeekProvider();
  if (deepseek) return [deepseek];
  const local = resolveLocalProvider();
  if (local) return [local];

  const models = await getModelRegistry();

  const tierOrder: ModelTier[] = preferTier === 'H'
    ? ['H', 'M', 'S']
    : preferTier === 'M'
      ? ['M', 'H', 'S']
      : ['S', 'M', 'H'];

  const pickBest = (provider: ProviderName): ModelEntry | undefined => {
    const available = models.filter((m) => m.provider === provider);
    for (const tier of tierOrder) {
      const match = available.find((m) => m.tier === tier);
      if (match) return match;
    }
    return available[0];
  };

  const candidates: ResolvedProvider[] = [];

  if (process.env.GROQ_API_KEY) {
    const m = pickBest('groq');
    if (m) candidates.push({ baseUrl: 'https://api.groq.com/openai/v1/chat/completions', model: m.id, headers: { Authorization: `Bearer ${process.env.GROQ_API_KEY}`, 'Content-Type': 'application/json' }, providerName: 'groq' });
  }
  if (process.env.CEREBRAS_API_KEY) {
    const m = pickBest('cerebras');
    if (m) candidates.push({ baseUrl: 'https://api.cerebras.ai/v1/chat/completions', model: m.id, headers: { Authorization: `Bearer ${process.env.CEREBRAS_API_KEY}`, 'Content-Type': 'application/json' }, providerName: 'cerebras' });
  }
  if (process.env.SAMBANOVA_API_KEY) {
    const m = pickBest('sambanova');
    if (m) candidates.push({ baseUrl: 'https://api.sambanova.ai/v1/chat/completions', model: m.id, headers: { Authorization: `Bearer ${process.env.SAMBANOVA_API_KEY}`, 'Content-Type': 'application/json' }, providerName: 'sambanova' });
  }
  if (process.env.MISTRAL_API_KEY) {
    const m = pickBest('mistral');
    if (m) candidates.push({ baseUrl: 'https://api.mistral.ai/v1/chat/completions', model: m.id, headers: { Authorization: `Bearer ${process.env.MISTRAL_API_KEY}`, 'Content-Type': 'application/json' }, providerName: 'mistral' });
  }
  if (process.env.OPENROUTER_API_KEY) {
    const m = pickBest('openrouter');
    if (m) candidates.push({ baseUrl: 'https://openrouter.ai/api/v1/chat/completions', model: m.id, headers: { Authorization: `Bearer ${process.env.OPENROUTER_API_KEY}`, 'Content-Type': 'application/json', 'HTTP-Referer': 'https://github.com/PW_alexpavsky', 'X-Title': 'PW AlexPavsky CI' }, providerName: 'openrouter' });
  }
  if (process.env.HF_TOKEN) {
    const m = pickBest('huggingface');
    if (m) candidates.push({ baseUrl: `https://api-inference.huggingface.co/models/${m.id}/v1/chat/completions`, model: m.id, headers: { Authorization: `Bearer ${process.env.HF_TOKEN}`, 'Content-Type': 'application/json' }, providerName: 'huggingface' });
  }
  if (process.env.GEMINI_API_KEY) {
    candidates.push({ baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions', model: 'gemini-3.5-flash', headers: { Authorization: `Bearer ${process.env.GEMINI_API_KEY}`, 'Content-Type': 'application/json' }, providerName: 'gemini' });
  }

  return candidates;
}

/**
 * Returns a summary of available providers and their best model.
 * Useful for debugging / logging in CI.
 */
export async function getProviderSummary(): Promise<string> {
  const deepseek = resolveDeepSeekProvider();
  if (deepseek) return `[model-registry] Provider summary:\n  deepseek     key=OK model=${deepseek.model}`;
  const local = resolveLocalProvider();
  if (local) {
    return `[model-registry] Provider summary:\n  ollama       endpoint=OK model=${local.model}`;
  }

  const models = await getModelRegistry();
  const providers: ProviderName[] = ['groq', 'cerebras', 'sambanova', 'mistral', 'openrouter', 'huggingface', 'gemini'];
  const lines: string[] = ['[model-registry] Provider summary:'];
  for (const p of providers) {
    const pModels = models.filter((m) => m.provider === p);
    const best = pModels[0];
    const hasKey = p === 'groq'        ? !!process.env.GROQ_API_KEY
      : p === 'openrouter'  ? !!process.env.OPENROUTER_API_KEY
      : p === 'huggingface' ? !!process.env.HF_TOKEN
      : p === 'cerebras'    ? !!process.env.CEREBRAS_API_KEY
      : p === 'sambanova'   ? !!process.env.SAMBANOVA_API_KEY
      : p === 'mistral'     ? !!process.env.MISTRAL_API_KEY
      : !!process.env.GEMINI_API_KEY;
    lines.push(`  ${p.padEnd(12)} key=${hasKey ? 'OK' : 'MISSING'} models=${pModels.length} best=${best?.id ?? 'none'}`);
  }
  return lines.join('\n');
}
