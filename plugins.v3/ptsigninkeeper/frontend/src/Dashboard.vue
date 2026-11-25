<script setup>
import './controls.css'
import { computed, h, onBeforeUnmount, onMounted, ref, resolveComponent, watch } from 'vue'
import KeeperButton from './KeeperButton.vue'
import { unpackPayload } from './config-model.mjs'

const props = defineProps({
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'PTSignInKeeper' },
  config: { type: Object, default: () => ({}) },
  allowRefresh: { type: Boolean, default: true },
})
const payload = ref(null)
const loading = ref(false)
const error = ref(false)
let disposed = false
let revision = 0
onBeforeUnmount(() => { disposed = true })
watch(() => props.config, value => {
  if (Array.isArray(value?.elements) && value.elements.length) {
    revision += 1
    payload.value = value
    error.value = false
  }
}, { immediate: true, deep: true })
const title = computed(() => payload.value?.attrs?.title || '签到与登录')
const pageUrl = computed(() => `#/plugin-app/${props.pluginId || 'PTSignInKeeper'}/main`)

async function load() {
  if (loading.value || disposed) return
  loading.value = true
  error.value = false
  const requestRevision = ++revision
  try {
    const result = unpackPayload(await props.api.get(
      `plugin/dashboard/${props.pluginId || 'PTSignInKeeper'}/ptsigninkeeper_dashboard`, { feedback: 'silent' }))
    if (!Array.isArray(result?.elements) || !result.elements.length) throw new Error('Empty dashboard')
    if (!disposed && requestRevision === revision) payload.value = result
  } catch { if (!disposed && requestRevision === revision) error.value = true }
  finally { if (!disposed) loading.value = false }
}
onMounted(() => { if (!payload.value) load() })

// The web widget consumes the same bounded JSON as native clients.
// Only the display components emitted by this plugin are supported.
const html = new Set(['div', 'strong'])
const vuetify = new Set(['VAlert', 'VChip', 'VDivider', 'VSpacer'])
function renderNode(node) {
  if (!node || (!html.has(node.component) && !vuetify.has(node.component))) return null
  const component = html.has(node.component) ? node.component : resolveComponent(node.component)
  if (node.text == null && !node.content?.length) return h(component, node.props || {})
  const children = () => [node.text == null ? null : String(node.text), ...(node.content || []).map(renderNode)]
  return h(component, node.props || {}, html.has(node.component) ? children() : { default: children })
}
const NativeContent = () => h('div', { class: 'widget-content' }, (payload.value?.elements || []).map(renderNode))
</script>

<template>
  <section class="signin-widget keeper-ui" :aria-busy="loading">
    <header><h3>{{ title }}</h3><KeeperButton v-if="allowRefresh" variant="tonal" icon="refresh" :loading="loading" :aria-label="loading ? '正在读取数据' : '重新读取数据'" @click="load" /></header>
    <p v-if="error" class="widget-error" role="status">{{ payload ? '读取失败，保留上次显示' : '暂时无法读取签到数据' }}<button type="button" @click="load">重试</button></p>
    <p v-if="loading && !payload" class="widget-loading" role="status">正在读取签到记录…</p>
    <NativeContent v-if="payload" />
    <footer><a :href="pageUrl">查看完整记录 <span aria-hidden="true">↗</span></a></footer>
  </section>
</template>

<style scoped>
.signin-widget{background:rgb(var(--v-theme-surface,255,255,255));color:rgb(var(--v-theme-on-surface,35,38,49));border:1px solid rgba(var(--v-theme-on-surface,35,38,49),.09);border-radius:14px;padding:19px 20px 14px;min-width:0;text-align:left}.signin-widget header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}.signin-widget h3{font-size:16px;font-weight:700;margin:0}.widget-content{overflow-wrap:anywhere;line-height:1.6}.widget-content :deep(.keeper-summary strong){font-size:24px;font-variant-numeric:tabular-nums}.widget-content :deep(.d-flex>strong){min-width:0}.widget-content :deep(.v-chip){flex-shrink:0}.widget-error,.widget-loading{font-size:12px;line-height:1.6;color:rgba(var(--v-theme-on-surface,35,38,49),.65)}.widget-error button{background:none;border:0;margin-left:8px;color:rgb(var(--v-theme-primary));cursor:pointer}.signin-widget footer{margin-top:14px;font-size:12px;text-align:right}.signin-widget a{color:rgb(var(--v-theme-primary));text-decoration:none}.signin-widget a:focus-visible,.widget-error button:focus-visible{outline:2px solid rgb(var(--v-theme-primary));outline-offset:3px}@media(max-width:360px){.signin-widget{padding:15px}}
</style>
