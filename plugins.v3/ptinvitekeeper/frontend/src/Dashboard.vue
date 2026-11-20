<script setup>
import { computed, onMounted, ref } from 'vue'
import { unpackPayload } from './config-model.mjs'

const props = defineProps({ api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: 'PTInviteKeeper' }, sourcePluginId: { type: String, default: '' }, config: { type: Object, default: () => ({}) }, allowRefresh: { type: Boolean, default: true } })
const overview = ref(null)
const loading = ref(false)
const error = ref(false)
const summary = computed(() => overview.value?.summary || {})
const rows = computed(() => (overview.value?.items || []).slice(0, 3))
const pageUrl = computed(() => `#/plugin-app/${props.pluginId || 'PTInviteKeeper'}/main`)
const updated = computed(() => {
  const value = Number(overview.value?.updated_at)
  if (!value) return '尚无数据快照'
  const date = new Date(value * 1000)
  return `数据更新于 ${date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })}`
})
async function load() {
  if (loading.value) return
  loading.value = true; error.value = false
  try {
    overview.value = unpackPayload(await props.api.get(`plugin/${props.pluginId || 'PTInviteKeeper'}/ui/overview`, { feedback: 'silent' }))
  } catch { error.value = true }
  finally { loading.value = false }
}
onMounted(load)
</script>

<template>
  <section class="invite-widget" :aria-busy="loading">
    <header><div><span class="widget-kicker">INVITATIONS</span><h3>{{ config.title || '邀请管理' }}</h3></div><button v-if="allowRefresh" type="button" :disabled="loading" :aria-label="loading ? '正在读取数据' : '重新读取数据'" @click="load"><span :class="{ rotating: loading }" aria-hidden="true">↻</span></button></header>
    <div v-if="error && !overview" class="widget-error" role="status">暂时无法读取邀请数据<button type="button" @click="load">重试</button></div>
    <template v-else>
      <div class="widget-stats">
        <div><strong>{{ summary.sites ?? '—' }}</strong><span>站点</span></div>
        <div class="good"><strong>{{ summary.invitable ?? '—' }}</strong><span>可邀请</span></div>
        <div :class="{ warning: summary.problem > 0 }"><strong>{{ summary.problem ?? '—' }}</strong><span>需处理</span></div>
        <div><strong>{{ summary.members ?? '—' }}</strong><span>成员</span></div>
      </div>
      <div class="widget-status"><span><i></i>剩余名额 <b>{{ summary.quota ?? '—' }}</b></span><a :href="pageUrl">查看全部 <span aria-hidden="true">↗</span></a></div>
      <div v-if="rows.length" class="widget-sites">
        <div v-for="row in rows" :key="row.id" class="widget-site"><span class="site-name" :title="row.name">{{ row.name }}</span><span :class="['site-state', row.needs_action ? 'warning' : row.can_invite ? 'good' : 'muted']">{{ row.label }}</span><span class="quota">{{ row.quota ?? '—' }} <small>名额</small></span></div>
      </div>
      <div v-else-if="!loading" class="widget-empty">执行一次刷新后，这里会显示邀请概况。</div>
      <footer><span :class="{ warning: error }">{{ error ? '读取失败，保留上次显示' : updated }}</span><span v-if="summary.low_ratio > 0">{{ summary.low_ratio }} 位成员分享率偏低</span></footer>
    </template>
  </section>
</template>

<style scoped>
.invite-widget{--iw-surface:rgb(var(--v-theme-surface,255,255,255));--iw-text:rgb(var(--v-theme-on-surface,35,38,49));--iw-muted:rgba(var(--v-theme-on-surface,35,38,49),.54);--iw-border:rgba(var(--v-theme-on-surface,35,38,49),.09);--iw-accent:rgb(var(--v-theme-primary));background:var(--iw-surface);color:var(--iw-text);border:1px solid var(--iw-border);border-radius:14px;padding:19px 20px 14px;font:inherit;text-align:left;min-width:0}.invite-widget header{display:flex;justify-content:space-between;align-items:center}.widget-kicker{display:block;font-size:9px;font-weight:700;letter-spacing:.14em;color:var(--iw-accent);margin-bottom:4px}.invite-widget h3{font-size:16px;font-weight:700;margin:0;letter-spacing:-.02em}.invite-widget header button{border:1px solid var(--iw-border);background:transparent;border-radius:7px;color:var(--iw-muted);width:28px;height:28px;cursor:pointer;font-size:19px;line-height:1}.invite-widget header button span{display:inline-block}.invite-widget button:disabled{opacity:.5;cursor:wait}.widget-stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:9px;margin:21px 0 18px}.widget-stats>div{display:flex;flex-direction:column;gap:5px;border-left:1px solid var(--iw-border);padding-left:12px}.widget-stats>div:first-child{border-left:0;padding-left:0}.widget-stats strong{font-size:25px;font-variant-numeric:tabular-nums;line-height:1.1;font-weight:680;letter-spacing:-.04em}.widget-stats span{font-size:11px;color:var(--iw-muted)}.good{color:rgb(var(--v-theme-success))}.warning{color:rgb(var(--v-theme-warning))}.muted{color:var(--iw-muted)}.widget-status{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:10px 0;border-top:1px solid var(--iw-border);font-size:11px;color:var(--iw-muted)}.widget-status>span{display:flex;align-items:center;gap:6px}.widget-status i{width:5px;height:5px;display:block;border-radius:50%;background:var(--iw-accent)}.widget-status b{font-variant-numeric:tabular-nums;font-weight:600;color:var(--iw-text)}.widget-status a{color:var(--iw-accent);text-decoration:none;font-size:11px}.widget-sites{border-top:1px solid var(--iw-border);padding-top:3px}.widget-site{display:grid;grid-template-columns:minmax(0,1fr) auto 60px;align-items:center;gap:10px;padding:8px 0;font-size:11px}.site-name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.site-state{font-size:10px;white-space:nowrap}.quota{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}.quota small{font-size:10px;color:var(--iw-muted);margin-left:3px}.invite-widget footer{display:flex;justify-content:space-between;flex-wrap:wrap;gap:5px;margin-top:9px;padding-top:11px;border-top:1px solid var(--iw-border);font-size:9px;color:var(--iw-muted)}.widget-empty,.widget-error{padding:18px 0;font-size:12px;color:var(--iw-muted);line-height:1.6}.widget-error button{color:var(--iw-accent);background:none;border:0;margin-left:8px;cursor:pointer}.rotating{animation:turn 1s linear infinite}.invite-widget a:focus-visible,.invite-widget button:focus-visible{outline:2px solid var(--iw-accent);outline-offset:3px}@keyframes turn{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.rotating{animation:none}}@media(max-width:360px){.invite-widget{padding:15px}.widget-stats strong{font-size:22px}.widget-site{grid-template-columns:minmax(0,1fr) auto}.quota{display:none}}
</style>
