/** Discard responses invalidated by navigation, filters, or component disposal. */
export function createRequestGate() {
  let generation = 0
  return { begin: () => ++generation, invalidate: () => ++generation, isCurrent: value => value === generation }
}

/** Cache only recent detail responses; cap retained entries for long sessions. */
export function createDetailCache(ttl, now = Date.now) {
  const entries = new Map()
  return {
    get(key) { const item = entries.get(key); if (!item || now() - item.time >= ttl) { entries.delete(key); return undefined }; return item.value },
    set(key, value) { entries.delete(key); entries.set(key, { value, time: now() }); if (entries.size > 24) entries.delete(entries.keys().next().value) },
    clear() { entries.clear() },
  }
}

/** Retain only non-sensitive display preferences, scoped to the plugin instance. */
export function readPreferences(key, storage) {
  try {
    storage ??= globalThis.localStorage
    const value = JSON.parse(storage?.getItem(key) || '{}')
    return { compact: value.compact !== false, pageSize: [10, 20, 50].includes(value.pageSize) ? value.pageSize : 20 }
  } catch { return { compact: true, pageSize: 20 } }
}
export function savePreferences(key, value, storage) {
  try { storage ??= globalThis.localStorage; storage?.setItem(key, JSON.stringify({ compact: value.compact !== false, pageSize: [10, 20, 50].includes(value.pageSize) ? value.pageSize : 20 })) } catch { /* Private browsing may disable storage. */ }
}
