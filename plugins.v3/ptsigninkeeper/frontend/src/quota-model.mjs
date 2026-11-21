import { unpackPayload } from './config-model.mjs'

export function formatUnits(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : '未知'
}

export function formatQuotaTime(value, seconds = false) {
  if (value == null || value === '') return '未知'
  const date = new Date(seconds ? value * 1000 : value)
  return Number.isNaN(date.getTime()) ? '未知' : date.toLocaleString('zh-CN', { hour12: false })
}

export function createQuotaQueries(request, changed = () => {}) {
  const states = new Map()
  let generation = 0
  let bulk = false
  const key = (token, url) => JSON.stringify([String(url || '').trim(), String(token || '').trim()])

  async function query(token, url) {
    token = String(token || '').trim()
    if (!token) return
    const id = key(token, url)
    if (states.get(id)?.loading) return
    const started = generation
    states.set(id, { loading: true }); changed()
    let result
    try {
      result = unpackPayload(await request({ token, browserless_url: String(url || '').trim() }))
      if (!result || !['ok', 'error', 'unsupported'].includes(result.status)) throw new Error('Invalid quota response')
    } catch {
      // Host/network exceptions can include request bodies; never render them.
      result = { status: 'error', message: '查询未完成，请检查连接后重试' }
    }
    if (started !== generation) return
    states.set(id, { ...result, loading: false }); changed()
  }

  return {
    query,
    state: (token, url) => states.get(key(token, url)) || {},
    get bulkLoading() { return bulk },
    reset() { generation += 1; states.clear(); bulk = false; changed() },
    async queryAll(tokens, url) {
      if (bulk) return
      const started = generation
      const pending = [...new Set(tokens.map(value => String(value || '').trim()).filter(Boolean))]
      let index = 0
      bulk = true; changed()
      try {
        await Promise.all(Array.from({ length: Math.min(3, pending.length) }, async () => {
          while (started === generation && index < pending.length) await query(pending[index++], url)
        }))
      } finally {
        if (started === generation) { bulk = false; changed() }
      }
    },
  }
}
