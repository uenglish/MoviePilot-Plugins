/** JSON-safe form helpers; preserve unrecognised configuration during UI upgrades. */
export const PLUGIN_KIND = 'invitee'
export const PLUGIN_ID = 'PTInviteKeeper'

const SIGNIN_DEFAULTS = {
  enabled: false, notify: true, rich_notify: true, cron: '', onlyonce: false,
  clean: false, queue_cnt: 5, sign_sites: [], login_sites: [],
  retry_keyword: '错误|失败', retry_time: '09:01', auto_cf: 0,
  read_activity: true, timeout: 30, retry_count: 1, retry_delay_minutes: 10,
  browserless_url: '', browserless_token: '', browserless_token2: '',
  browserless_token3: '', browserless_token4: '', browserless_timeout: 100, site_rules: '',
}
const INVITEE_DEFAULTS = {
  enabled: false, notify: false, cron: '0 9 * * *', onlyonce: false,
  site_ids: ['all'], no_browser_sites: [], page_filter: '',
}

export function cloneConfig(value) {
  return JSON.parse(JSON.stringify(value || {}))
}

export function makeConfig(initial, kind = PLUGIN_KIND) {
  return { ...cloneConfig(kind === 'signin' ? SIGNIN_DEFAULTS : INVITEE_DEFAULTS), ...cloneConfig(initial) }
}

export function selection(values) {
  if (Array.isArray(values)) return values.filter(value => value !== null && value !== undefined && typeof value !== 'boolean')
  if (typeof values === 'string') {
    try { return selection(JSON.parse(values)) } catch { return values.split(/[,，;；\s]+/).filter(Boolean) }
  }
  return values === null || values === undefined ? [] : [values]
}

export function isAll(values, emptyMeansAll = false) {
  const items = selection(values)
  return items.some(value => String(value).toLowerCase() === 'all') || (emptyMeansAll && !items.length)
}

export function toggleSelection(values, value, checked) {
  const key = String(value)
  const result = selection(values).filter(item => String(item).toLowerCase() !== 'all' && String(item) !== key)
  return checked ? [...result, value] : result
}

export function selectVisible(values, options, checked) {
  return options.reduce((result, option) => toggleSelection(result, option.id, checked), selection(values))
}

export function unpackPayload(payload) {
  if (payload && typeof payload.success === 'boolean') {
    if (!payload.success) throw new Error(payload.message || '读取失败，请稍后重试')
    return payload.data ?? (payload.message ? { message: payload.message } : {})
  }
  return payload || {}
}

export function validateConfig(config, kind = PLUGIN_KIND) {
  const errors = {}
  const cron = String(config.cron || '').trim()
  if (cron && cron.split(/\s+/).length !== 5 && !(kind === 'signin' && /^\d+(?:\.\d+)?(?:\/(?:\d{1,2})-(?:\d{1,2}))?$/.test(cron))) {
    errors.cron = kind === 'signin' ? '填写 5 段 Cron，或小时周期，例如 2.3/9-23' : '填写 5 段 Cron，例如 0 9 * * *'
  }
  if (kind === 'signin') {
    const retry = String(config.retry_time || '').trim()
    if (retry && !/^(?:[01]?\d|2[0-3]):[0-5]\d$/.test(retry)) errors.retry_time = '填写有效的 HH:MM，留空可关闭'
    const limits = { queue_cnt: [1, 20], timeout: [5, 120], retry_count: [0, 5], retry_delay_minutes: [1, 1440], browserless_timeout: [15, 300], auto_cf: [0, 1000] }
    for (const [key, [minimum, maximum]] of Object.entries(limits)) {
      const value = Number(config[key])
      if (config[key] === '' || !Number.isFinite(value) || !Number.isInteger(value) || value < minimum || value > maximum) errors[key] = `填写 ${minimum}–${maximum} 之间的整数`
    }
    if (String(config.site_rules || '').trim()) {
      try { JSON.parse(config.site_rules) } catch { errors.site_rules = '规则不是有效 JSON，请检查逗号、引号和括号' }
    }
  }
  return errors
}
