<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick, defineAsyncComponent } from 'vue'
import { createRequestGate, createDetailCache, readPreferences, savePreferences } from './view-state.mjs'
const SettingsPage = defineAsyncComponent(() => import('./SettingsPage.vue'))
import SiteAvatar from './SiteAvatar.vue'
import { PLUGIN_ID, PLUGIN_KIND, unpackPayload } from './config-model.mjs'

const props = defineProps({ api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: '' }, sourcePluginId: { type: String, default: '' }, navKey: { type: String, default: '' } })
const emit = defineEmits(['switch', 'action', 'close'])
const signin = PLUGIN_KIND === 'signin'
const id = computed(() => props.pluginId || PLUGIN_ID)
const model = ref({ items: [], summary: {}, categories: [], pages: 1, total: 0 })
const loading = ref(true), error = ref(''), notice = ref(''), query = ref(''), category = ref('all')
const page = ref(1), pageSize = ref(20), sort = ref('priority'), day = ref(''), includeInactive = ref(false)
const compact = ref(true), expanded = ref(''), detail = ref(null), detailLoading = ref(false), detailError = ref('')
const memberQuery = ref(''), memberStatus = ref('all'), memberPage = ref(1), actionPending = ref(false)
const panel = ref(null), tableScroll = ref(null), showSettings = ref(false)
const listGate = createRequestGate(), detailGate = createRequestGate(), detailCache = createDetailCache(30000)
const pluginTitle = signin ? 'PT 签到管家' : 'PT 邀请管家'
const preferencesKey = computed(() => `pt-tools:${id.value}:view`)
const historical = computed(() => signin && Boolean(day.value) && day.value !== model.value.today)
const csvFile = ref(null)
function releaseExport() {
  if (csvFile.value) URL.revokeObjectURL(csvFile.value.url)
  csvFile.value = null
}
function dismissNotice() { notice.value = ''; releaseExport() }
let initialList = true
let searchTimer, memberTimer, pollTimer, disposed = false
const busy = computed(() => actionPending.value || model.value.task?.running || model.value.refreshing)
const summary = computed(() => model.value.summary || {})
const totalSites = computed(() => signin ? summary.value.total_sites : summary.value.sites)
const metrics = computed(() => signin ? [
  { label: '签到完成', value: summary.value.success, tone: 'success', key: 'success', note: '收到成功或已签到回执' },
  { label: '待处理', value: (summary.value.failed || 0) + (summary.value.pending || 0), tone: 'error', key: 'problem', note: '失败与尚未确认的站点' },
  { label: '登录有效', value: summary.value.logged_in, tone: 'info', key: 'logged_in', note: '签到与登录分别核验' },
  { label: '取得奖励', value: summary.value.rewards, tone: 'accent', key: 'rewards', note: '以实际奖励回执为准' },
] : [
  { label: '可邀请站点', value: summary.value.invitable, tone: 'success', key: 'invitable', note: '当前具备邀请条件' },
  { label: '剩余名额', value: summary.value.quota, tone: 'accent', key: 'all', note: `永久 ${number(summary.value.permanent)} · 临时 ${number(summary.value.temporary)}` },
  { label: '邀请成员', value: summary.value.members, tone: 'info', key: 'all', note: `低分享率 ${number(summary.value.low_ratio)} · 禁用 ${number(summary.value.banned)}` },
  { label: '待处理', value: summary.value.problem, tone: 'error', key: 'problem', note: '连接、验证或数据读取异常' },
])
const tabs = computed(() => signin ? [
  { key: 'all', label: '全部站点', count: totalSites.value },
  { key: 'problem', label: '待处理', count: (summary.value.failed || 0) + (summary.value.pending || 0) },
  { key: 'success', label: '签到完成', count: summary.value.success },
  { key: 'no_sign', label: '仅登录站点', count: summary.value.no_sign },
] : [
  { key: 'all', label: '全部站点', count: totalSites.value },
  { key: 'invitable', label: '可邀请', count: summary.value.invitable },
  { key: 'problem', label: '待处理', count: summary.value.problem },
  { key: 'stale', label: '数据超期', count: summary.value.stale },
])
const customCategories = computed(() => signin ? [
  { key: 'failed', label: '签到失败' }, { key: 'pending', label: '未确认 / 未执行' },
  { key: 'inactive', label: '已停用' }, { key: 'logged_in', label: '登录有效' }, { key: 'rewards', label: '取得奖励' },
] : (model.value.categories || []).filter(c => c.count > 0 && !['invitable'].includes(c.key)))
const rowRange = computed(() => model.value.total ? `${(model.value.page - 1) * model.value.page_size + 1}–${Math.min(model.value.page * model.value.page_size, model.value.total)}` : '0')
const pageNumbers = computed(() => { const count = model.value.pages || 1; const start = Math.max(1, Math.min(page.value - 2, count - 4)); return Array.from({ length: Math.min(count, 5) }, (_, i) => start + i) })

function number(value) { return value === null || value === undefined || value === '' || !Number.isFinite(Number(value)) ? '—' : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(Number(value)) }
function color(value) { return ['success', 'error', 'warning', 'info', 'accent'].includes(value) ? value : value === 'primary' ? 'accent' : 'neutral' }
function rowTone(row) {
  if (signin) return color(row.color || row.signin_status?.color)
  if (row.cached_after_failure) return 'error'
  if (['unknown', 'quota', 'level', 'closed', 'limit', 'platform'].includes(row.category)) return 'warning'
  if (row.needs_action) return 'error'
  return row.category === 'invitable' ? 'success' : 'neutral'
}
function symbol(tone) { return ({ success: '✓', error: '×', warning: '!', info: '•', neutral: '−', accent: '•' })[color(tone)] }
function domain(url) { try { return new URL(url).hostname.replace(/^www\./, '') } catch { return '' } }
function timestamp(value) { if (!value) return '暂无时间记录'; const date = new Date(typeof value === 'number' ? value * 1000 : value); return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) }
function relative(value) { if (!value) return '尚未获取'; const diff = Math.max(0, Date.now() / 1000 - value); return diff < 60 ? '刚刚更新' : diff < 3600 ? `${Math.floor(diff / 60)} 分钟前` : diff < 86400 ? `${Math.floor(diff / 3600)} 小时前` : `${Math.floor(diff / 86400)} 天前` }
function activity(value) { return String(value || '').replace(/\s*\([^)]*(?:秒|分|天|月|年).*\)\s*$/, '').trim() || '未获取' }
function qs(values) { return new URLSearchParams(Object.entries(values).filter(([, v]) => v !== undefined && v !== null && v !== '')).toString() }
async function get(path, values = {}) { return unpackPayload(await props.api.get(`plugin/${id.value}/ui/${path}?${qs(values)}`, { feedback: 'silent' })) }
function closeDetail() {
  detailGate.invalidate(); expanded.value = ''; detail.value = null; detailError.value = ''; detailLoading.value = false
  clearTimeout(memberTimer)
}
function resetPage() { page.value = 1; closeDetail() }
function selectCategory(value) { category.value = value }
function schedulePoll() {
  clearTimeout(pollTimer)
  if (!disposed && !document.hidden && (model.value.task?.running || model.value.refreshing))
    pollTimer = setTimeout(() => load({ quiet: true }), 3000)
}
async function load({ quiet = false } = {}) {
  if (disposed) return
  const current = listGate.begin()
  if (!quiet) loading.value = true
  error.value = ''; clearTimeout(pollTimer)
  try {
    const response = await get('data', { query: query.value, category: category.value, sort: sort.value, page: page.value, page_size: pageSize.value, day: signin ? day.value : '', include_inactive: signin ? includeInactive.value : undefined })
    if (!listGate.isCurrent(current) || disposed) return
    if (initialList) {
      initialList = false
      if (!signin && response.default_query && !query.value) query.value = String(response.default_query)
    }
    const wasBusy = model.value.task?.running || model.value.refreshing
    model.value = response; page.value = response.page || 1
    if (wasBusy && !response.task?.running && !response.refreshing) {
      detailCache.clear()
      if (expanded.value) scheduleDetail()
    }
    schedulePoll()
  } catch { if (listGate.isCurrent(current)) error.value = '数据暂时无法加载。请重试，或检查插件是否启用。' }
  finally { if (listGate.isCurrent(current)) loading.value = false }
}
function scheduleDetail() {
  clearTimeout(memberTimer)
  memberTimer = setTimeout(loadDetail, 180)
}
function toggle(row) {
  if (expanded.value === row.id) { closeDetail(); return }
  detailGate.invalidate(); expanded.value = row.id; detail.value = null; detailError.value = ''
  memberQuery.value = ''; memberStatus.value = 'all'; memberPage.value = 1
  scheduleDetail()
}
async function loadDetail() {
  if (!expanded.value || disposed) return
  const current = detailGate.begin()
  const values = { site: expanded.value, day: signin ? (day.value || model.value.date) : '', member_query: memberQuery.value, member_status: memberStatus.value, page: memberPage.value, page_size: 10 }
  const key = JSON.stringify(values)
  detailLoading.value = true; detailError.value = ''
  try {
    const response = detailCache.get(key) ?? await get('detail', values)
    if (!detailGate.isCurrent(current) || disposed) return
    detailCache.set(key, response); detail.value = response
  } catch { if (detailGate.isCurrent(current)) detailError.value = '详情加载失败，请重试。' }
  finally { if (detailGate.isCurrent(current)) detailLoading.value = false }
}
function visibilityChanged() {
  if (document.hidden) clearTimeout(pollTimer)
  else if (busy.value) load({ quiet: true })
}
async function settingsClosed(saved = false) {
  showSettings.value = false
  if (saved) { detailCache.clear(); await load({ quiet: true }); if (expanded.value) scheduleDetail() }
}
async function run(scope = 'pending', site = '') {
  if (busy.value || historical.value) return
  actionPending.value = true; dismissNotice()
  try {
    const result = unpackPayload(await props.api.post(`plugin/${id.value}/ui/run?${qs({ scope, site })}`, {}, { feedback: 'silent' }))
    detailCache.clear(); notice.value = result.message || '任务已开始，结果会自动更新。'; await load({ quiet: true })
  } catch { notice.value = '任务未能启动，请稍后重试。' }
  finally { actionPending.value = false }
}
async function exportData() {
  releaseExport()
  try {
    const data = await get('export', { query: query.value, category: category.value, day: day.value, include_inactive: includeInactive.value })
    const rows = Array.isArray(data) ? data : data.items || []
    if (!rows.length) { notice.value = '没有可导出的记录。'; return }
    const quote = value => { const text = String(value ?? ''); return `"${(/^[=+@-]/.test(text) ? "'" + text : text).replaceAll('"', '""')}"` }
    const keys = Object.keys(rows[0]), csv = '\ufeff' + [keys.map(quote).join(','), ...rows.map(row => keys.map(k => quote(row[k])).join(','))].join('\r\n')
    csvFile.value = {
      url: URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8;' })),
      name: `${signin ? '签到记录' : '邀请概况'}-${(signin && model.value.date) || new Date().toLocaleDateString('sv-SE')}.csv`,
    }
    notice.value = `已生成 ${rows.length} 条记录。`
  } catch { notice.value = '导出失败，请重试。' }
}
async function changePage(value) {
  page.value = value; closeDetail(); await load(); await nextTick()
  if (tableScroll.value) tableScroll.value.scrollTop = 0
  panel.value?.scrollIntoView({ block: 'start', behavior: 'instant' })
}
function goSettings() { showSettings.value = true }
watch([query, category, sort, pageSize, day, includeInactive], (values, previous) => {
  listGate.invalidate(); detailGate.invalidate(); clearTimeout(searchTimer); resetPage()
  searchTimer = setTimeout(() => load(), values[0] !== previous[0] ? 250 : 0)
})
watch([memberQuery, memberStatus], () => { detailGate.invalidate(); memberPage.value = 1; scheduleDetail() })
watch([compact, pageSize], () => savePreferences(preferencesKey.value, { compact: compact.value, pageSize: pageSize.value }))
onMounted(() => {
  const preferences = readPreferences(preferencesKey.value)
  compact.value = preferences.compact; pageSize.value = preferences.pageSize
  document.addEventListener('visibilitychange', visibilityChanged)
  // The single list scheduler coalesces restoration and initial loading.
  clearTimeout(searchTimer); searchTimer = setTimeout(() => load(), 0)
})
onBeforeUnmount(() => {
  releaseExport(); disposed = true; listGate.invalidate(); detailGate.invalidate()
  clearTimeout(searchTimer); clearTimeout(memberTimer); clearTimeout(pollTimer); detailCache.clear()
  document.removeEventListener('visibilitychange', visibilityChanged)
})
</script>

<template>
  <SettingsPage v-if="showSettings" :api="api" :plugin-id="id" :source-plugin-id="sourcePluginId" embedded @close="settingsClosed" />
  <section v-show="!showSettings" class="mp-desk pt-signin-page" :class="{ compact, 'invite-desk': !signin }">
    <header class="desk-heading">
      <div class="heading-identity">
        <VIcon :icon="signin ? 'mdi-calendar-check-outline' : 'mdi-account-group-outline'" color="primary" size="28" />
        <div class="heading-copy"><h1>{{ pluginTitle }}</h1><p>{{ number(totalSites) }} 个{{ signin ? '启用' : '已记录' }}站点<span class="separator">·</span>{{ signin ? `${model.date || '今日'} 的签到与登录` : `最近更新 ${timestamp(model.updated_at)}` }}</p></div>
      </div>
      <div class="heading-actions">
        <VTooltip text="读取已保存的最新结果，不访问 PT 站点">
          <template #activator="{ props: tipProps }"><VBtn v-bind="tipProps" variant="text" color="primary" icon="mdi-refresh" :loading="loading" aria-label="刷新显示" @click="load()" /></template>
        </VTooltip>
        <VTooltip text="插件设置">
          <template #activator="{ props: tipProps }"><VBtn v-bind="tipProps" variant="text" color="primary" icon="mdi-tune-variant" aria-label="打开插件设置" @click="goSettings" /></template>
        </VTooltip>
        <VTooltip :text="signin ? '访问未完成、失败或登录异常的站点，执行签到与登录' : '访问异常站点，重新获取邀请资格与成员数据'">
          <template #activator="{ props: tipProps }"><VBtn v-bind="tipProps" variant="tonal" color="primary" prepend-icon="mdi-play" :loading="busy" :disabled="busy || historical" @click="run('pending')">{{ busy ? '任务进行中' : historical ? '历史记录' : signin ? '执行签到' : '重试异常' }}</VBtn></template>
        </VTooltip>
      </div>
    </header>

    <div v-if="historical" class="history-notice">正在查看 {{ model.date }} 的历史记录。<button class="text-button" @click="day = ''">返回今天</button></div>
    <div class="metric-strip" aria-label="站点概况">
      <VSheet tag="button" type="button" v-for="metric in metrics" :key="metric.label" class="metric app-surface-static" :aria-label="`${metric.label} ${number(metric.value)}，查看站点`" :class="`tone-${metric.tone}`" @click="selectCategory(metric.key)"><span class="metric-label"><i class="dot"></i>{{ metric.label }}</span><strong>{{ number(metric.value) }}<small>{{ !signin && metric.label === '剩余名额' ? '个' : !signin && metric.label === '邀请成员' ? '人' : '站' }}</small></strong><span class="metric-note">{{ metric.note }}</span></VSheet>
    </div>
    <div v-if="notice || busy" class="notice" role="status"><span>{{ busy ? '正在访问站点，结果会自动更新；可以继续筛选与查看记录。' : notice }}</span><a v-if="csvFile && !busy" class="csv-download" :href="csvFile.url" :download="csvFile.name">下载 CSV 文件</a><button v-if="!busy" aria-label="关闭提示" @click="dismissNotice">×</button></div>

    <VSheet class="data-panel app-surface-static"><div ref="panel">
      <div class="panel-heading"><div><h3>站点列表</h3><span>{{ signin ? '核对签到、登录与奖励' : '查看邀请资格、名额与成员' }}</span></div><span class="record-count">{{ model.total || 0 }} 个结果</span><button v-if="expanded" class="text-button" @click="closeDetail">收起详情</button></div>
      <div class="panel-tabs"><div class="tab-list" role="group" aria-label="站点状态筛选"><button v-for="tab in tabs" :key="tab.key" :aria-pressed="category === tab.key" :class="{ selected: category === tab.key }" @click="selectCategory(tab.key)">{{ tab.label }}<span>{{ tab.count || 0 }}</span></button></div><div class="legend"><span><i class="dot success"></i>成功</span><span><i class="dot error"></i>{{ signin ? '失败' : '需处理' }}</span><span><i class="dot warning"></i>{{ signin ? '未确认' : '受限' }}</span></div></div>
      <div class="toolbar">
        <VTextField v-model="query" class="search-field" type="search" placeholder="搜索站点或原因" aria-label="搜索站点或原因" prepend-inner-icon="mdi-magnify" variant="outlined" density="compact" hide-details :loading="loading" />
        <VSelect v-if="signin" v-model="day" class="filter-field" :items="[{ title: '今天', value: '' }, ...(model.days || []).filter(d => (typeof d === 'string' ? d : d.value) !== model.today).map(d => ({ title: typeof d === 'string' ? d : d.label, value: typeof d === 'string' ? d : d.value }))]" aria-label="记录日期" variant="outlined" density="compact" hide-details />
        <VSelect :model-value="customCategories.some(c => c.key === category) ? category : ''" class="filter-field" :items="[{ title: '更多状态', value: '' }, ...customCategories.map(c => ({ title: c.label + (c.count === undefined ? '' : ` (${c.count})`), value: c.key }))]" aria-label="更多状态" variant="outlined" density="compact" hide-details @update:model-value="category = $event || 'all'" />
        <VSelect v-model="sort" class="filter-field" :items="[{ title: '站点顺序', value: 'priority' }, { title: '异常优先', value: 'status' }, { title: '名称排序', value: 'name' }, ...(signin ? [{ title: '奖励数值', value: 'reward' }] : [{ title: '名额最多', value: 'quota' }, { title: '成员最多', value: 'members' }, { title: '最近更新', value: 'updated' }])]" aria-label="排序" variant="outlined" density="compact" hide-details />
        <div class="toolbar-spacer"></div><VBtn variant="text" color="primary" :aria-pressed="compact" :prepend-icon="compact ? 'mdi-format-line-spacing' : 'mdi-format-line-spacing'" @click="compact = !compact">{{ compact ? '紧凑' : '舒适' }}</VBtn><VBtn variant="text" color="primary" prepend-icon="mdi-download" @click="exportData">导出</VBtn>
      </div>
      <div v-if="query || category !== 'all'" class="filter-note"><span>当前筛选：{{ query || '全部关键词' }}<span v-if="category !== 'all'"> · {{ [...tabs, ...customCategories].find(c => c.key === category)?.label }}</span></span><button class="text-button" @click="query = ''; category = 'all'">清除筛选</button></div>
      <div v-if="signin" class="scope-note">登录有效仅代表会话可用；请结合最近访问记录与站点规则判断保活。</div>
      <div v-if="signin && summary.inactive" class="scope-note"><label><input v-model="includeInactive" type="checkbox"> 显示 {{ summary.inactive }} 个已停用站点</label><span>已停用站点不计入失败统计</span></div>
      <div v-if="error" class="empty-state error-state" role="alert"><strong>暂时无法加载</strong><p>{{ error }}</p><VBtn variant="tonal" color="primary" @click="load()">重新加载</VBtn></div>
      <div v-else-if="loading && !model.items?.length" class="loading-skeleton" aria-label="正在加载"><div v-for="i in 6" :key="i"></div></div>
      <div v-else-if="!model.items?.length" class="empty-state"><span class="empty-symbol">✓</span><strong>{{ category === 'problem' ? '没有需要处理的站点' : '没有匹配的站点' }}</strong><p>可以调整筛选条件，或查看全部站点。</p><VBtn variant="tonal" color="primary" @click="query = ''; category = 'all'">查看全部</VBtn></div>
      <div v-else ref="tableScroll" class="table-scroll" :aria-busy="loading">
        <table class="site-table"><thead><tr><th class="site-column">站点</th><template v-if="signin"><th>签到状态</th><th>登录状态</th><th class="amount">本次奖励</th><th class="wide-column">最近访问</th></template><template v-else><th>邀请状态</th><th class="amount">剩余名额</th><th class="amount">成员</th><th class="wide-column">状态说明</th><th>更新时间</th></template><th class="action-column"><span class="sr-only">操作</span></th></tr></thead>
          <tbody><template v-for="row in model.items" :key="row.id">
            <tr :class="[`row-${rowTone(row)}`, { 'is-expanded': expanded === row.id }]">
              <td class="identity-cell" data-label="站点"><button class="site-button" :aria-expanded="expanded === row.id" :aria-label="`${expanded === row.id ? '收起' : '展开'} ${row.name} 详情`" @click="toggle(row)"><span class="row-chevron" :class="{ open: expanded === row.id }">›</span><SiteAvatar :api="api" :site-id="signin ? row.id : row.site_id" :name="row.name" /><span class="site-identity"><strong>{{ row.name }}</strong><small>{{ domain(row.url) || (row.active === false ? '已停用' : '站点记录') }}</small></span></button></td>
              <template v-if="signin"><td data-label="签到"><span :title="row.message" class="status-badge" :class="`tone-${color(row.signin_status?.color || row.color)}`"><b>{{ symbol(row.signin_status?.color || row.color) }}</b>{{ row.signin_status?.label || row.label }}</span></td><td data-label="登录"><span class="status-badge" :class="`tone-${color(row.login_status?.color)}`"><b>{{ symbol(row.login_status?.color) }}</b>{{ row.login_status?.label || '未执行' }}</span></td><td class="amount reward" data-label="本次奖励" :class="{ muted: !row.bonus || ['—', '未获取'].includes(row.bonus) }">{{ row.bonus || '未获取' }}</td><td class="result-cell" data-label="最近访问"><div class="result-line" :title="activity(row.activity)">{{ activity(row.activity) }}</div><small v-if="row.logged_in" class="row-subline">{{ row.visit_label || '保活未确认' }}</small></td></template>
              <template v-else><td data-label="邀请状态"><span class="status-badge" :class="`tone-${color(row.color)}`"><b>{{ symbol(row.color) }}</b>{{ row.label }}</span></td><td class="amount" data-label="剩余名额"><strong>{{ number(row.quota) }}</strong><small class="row-subline">{{ number(row.permanent) }} 永久 / {{ number(row.temporary) }} 临时</small></td><td class="amount" data-label="成员"><strong>{{ row.cached_after_failure && !row.updated_at ? '—' : row.members }}</strong><small v-if="row.low_ratio || row.banned" class="row-subline danger-text">{{ row.low_ratio + row.banned }} 人需关注</small></td><td class="result-cell" data-label="状态说明"><div class="result-line" :title="row.reason">{{ row.reason || '暂无说明' }}</div><small v-if="row.cached_after_failure" class="row-subline danger-text">获取失败 · 数值为历史缓存</small></td><td data-label="更新时间"><span class="time-cell" :title="timestamp(row.updated_at)">{{ row.cached_after_failure ? '获取失败' : relative(row.updated_at) }}</span><small v-if="row.stale" class="row-subline danger-text">{{ row.updated_at ? '已超过 12 小时' : '暂无成功时间' }}</small></td></template>
              <td class="row-actions"><VBtn variant="text" color="primary" size="small" class="row-detail-button" :aria-label="`查看 ${row.name} 详情`" @click="toggle(row)">{{ expanded === row.id ? '收起' : '详情' }}</VBtn></td>
            </tr>
            <tr v-if="expanded === row.id" class="detail-row"><td :colspan="signin ? 6 : 7"><div class="detail-panel">
              <div class="detail-heading"><div><strong>{{ row.name }}</strong><span>{{ signin ? '近 14 天记录' : '邀请详情与成员健康' }}</span></div><div class="detail-actions"><a v-if="row.url" :href="row.url" target="_blank" rel="noopener noreferrer">打开站点 ↗</a><VBtn variant="tonal" color="primary" :disabled="busy || historical || row.active === false" @click="run('site', row.id)">{{ signin ? '重试此站' : '重新获取此站' }}</VBtn><button class="icon-button" aria-label="收起详情" @click="closeDetail">×</button></div></div>
              <div v-if="detailLoading && !detail" class="detail-loading"><span class="spinner"></span>正在读取详情…</div><div v-if="detailError" class="detail-error">{{ detailError }}<VBtn variant="tonal" color="primary" @click="loadDetail">重试</VBtn></div>
              <template v-if="detail && signin"><div class="receipt" :class="`tone-${rowTone(row)}`"><strong>最近回执</strong><p>{{ row.message || '暂无回执' }}</p></div><details class="history-fold"><summary>签到与登录历史 <span>{{ detail.history?.length || 0 }} 天</span></summary><div class="history-list"><div v-for="record in detail.history" :key="record.day" class="history-item"><time>{{ record.day }}</time><span class="status-badge" :class="`tone-${color(record.signin_status?.color)}`">{{ symbol(record.signin_status?.color) }} {{ record.signin_status?.label || '未执行' }}</span><p :title="record.sign?.message">{{ record.sign?.message || '暂无签到记录' }}<small>登录：{{ record.login?.message || (record.sign?.logged_in ? '已确认' : '未记录') }}</small></p><strong class="reward">{{ record.signin_status?.color === 'success' ? record.sign?.bonus ?? '未获取奖励' : '—' }}</strong></div><p v-if="!detail.history?.length" class="muted">暂无历史记录</p></div></details></template>
              <template v-if="detail && !signin"><div class="invite-facts"><div><span>永久 / 临时名额</span><strong>{{ number(row.permanent) }} / {{ number(row.temporary) }}</strong></div><div><span>当前魔力</span><strong>{{ number(row.bonus) }}</strong></div><div><span>兑换单价（永久 / 临时）</span><strong>{{ number(row.permanent_price) }} / {{ number(row.temporary_price) }}</strong></div><div><span>可兑换（按魔力估算）</span><strong>{{ number(row.purchasable_permanent) }} / {{ number(row.purchasable_temporary) }}</strong></div></div><div class="receipt" :class="`tone-${rowTone(row)}`"><strong>{{ row.label }}</strong><p>{{ row.reason }}<span v-if="row.advice"> · {{ row.advice }}</span></p></div>
                <div class="member-heading"><strong>邀请成员 <span class="muted">{{ detail.total }} 人</span></strong><input v-model="memberQuery" type="search" placeholder="搜索成员…" aria-label="搜索成员"><select v-model="memberStatus" aria-label="成员状态"><option value="all">全部成员</option><option value="low_ratio">低分享率</option><option value="banned">已禁用</option><option value="no_data">数据不全</option><option value="healthy">状态正常</option></select></div>
                <div v-if="!detail.items?.length" class="member-empty">{{ memberQuery || memberStatus !== 'all' ? '没有匹配的成员' : row.needs_action ? '本次尚未取得有效成员数据，请查看上方原因。' : '站点未返回成员记录。' }}</div>
                <div v-else class="member-scroll"><table class="member-table"><thead><tr><th>成员</th><th>分享率</th><th>上传 / 下载</th><th>做种数量</th><th>做种体积</th><th>状态</th></tr></thead><tbody><tr v-for="member in detail.items" :key="member.username"><td><a v-if="member.profile_url" :href="member.profile_url" target="_blank" rel="noopener noreferrer">{{ member.username }} ↗</a><strong v-else>{{ member.username }}</strong><small class="row-subline">{{ member.last_seed_report }}</small></td><td :class="member.health === 'low_ratio' ? 'danger-text' : ''">{{ member.ratio ?? '—' }}</td><td>{{ member.uploaded || '—' }} / {{ member.downloaded || '—' }}</td><td>{{ member.seeding ?? '—' }}</td><td>{{ member.seeding_size || '—' }}</td><td><span class="status-badge" :class="`tone-${member.health === 'healthy' ? 'success' : member.health === 'no_data' ? 'neutral' : 'error'}`">{{ member.banned ? '已禁用' : member.ratio_label }}</span></td></tr></tbody></table></div>
                <div v-if="detail.pages > 1" class="member-pagination"><VBtn variant="tonal" color="primary" :disabled="memberPage <= 1" @click="memberPage--; loadDetail()">上一页</VBtn><span>{{ memberPage }} / {{ detail.pages }}</span><VBtn variant="tonal" color="primary" :disabled="memberPage >= detail.pages" @click="memberPage++; loadDetail()">下一页</VBtn></div>
                <details v-if="detail.history?.length" class="trend-details"><summary>查看该站最近 {{ detail.history.length }} 次记录</summary><div class="trend-points"><div v-for="(point, index) in detail.history" :key="index"><time>{{ timestamp(point.time) }}</time><span>{{ model.categories?.find(c => c.key === point.category)?.label || point.category || '' }}</span><strong>{{ point.quota ?? point.members ?? '—' }}</strong></div></div></details>
              </template>
            </div></td></tr>
          </template></tbody>
        </table>
      </div>
      <footer class="pagination"><span>显示 {{ rowRange }} 条，共 {{ model.total || 0 }} 条</span><label>每页 <select v-model.number="pageSize" aria-label="每页条数"><option :value="10">10</option><option :value="20">20</option><option :value="50">50</option></select> 条</label><nav aria-label="分页"><button :disabled="loading || page <= 1" aria-label="上一页" @click="changePage(page - 1)">‹</button><button v-for="num in pageNumbers" :key="num" :class="{ current: page === num }" :aria-label="`第 ${num} 页`" :aria-current="page === num ? 'page' : undefined" @click="changePage(num)">{{ num }}</button><button :disabled="loading || page >= (model.pages || 1)" aria-label="下一页" @click="changePage(page + 1)">›</button></nav></footer>
    </div></VSheet>
    <div class="desk-footnote"><span>{{ signin ? '奖励以站点回执为准；“未获取”不表示 0。历史按站点展开读取。' : '等级不足、邀请关闭属于站点规则；红色标记需要检查的读取异常。' }}</span><button v-if="!signin" class="text-button" :disabled="busy" @click="run('all')">重新获取全部站点</button></div>
  </section>
</template>

<style scoped src="./dashboard.css"></style>

<style src="./host-layout.css"></style>
