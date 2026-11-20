import { unpackPayload } from './config-model.mjs'

const caches = new WeakMap()

export function iconSource(value) {
  const source = typeof value === 'string' ? value.trim() : ''
  if (/^data:image\/[a-z0-9.+-]+;base64,[a-z0-9+/=\s]+$/i.test(source)) return source
  try {
    const url = new URL(source)
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? source : ''
  } catch { return '' }
}

export async function loadSiteIcon(api, siteId) {
  const id = String(siteId ?? '')
  if (!/^[1-9]\d*$/.test(id) || typeof api?.get !== 'function') return ''
  let cache = caches.get(api)
  if (!cache) { cache = new Map(); caches.set(api, cache) }
  const old = cache.get(id)
  if (old && old.expires > Date.now()) return old.promise
  const promise = Promise.resolve().then(() => api.get(`site/icon/${id}`, { feedback: 'silent' }))
    .then(response => iconSource(unpackPayload(response).icon)).catch(() => '')
  cache.delete(id)
  cache.set(id, { expires: Date.now() + 300000, promise })
  while (cache.size > 128) cache.delete(cache.keys().next().value)
  return promise
}
