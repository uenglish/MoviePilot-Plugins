<script setup>
import { computed, onMounted, ref } from 'vue'
import Config from './Config.vue'
import { PLUGIN_ID, cloneConfig, unpackPayload } from './config-model.mjs'

const props = defineProps({ api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: '' }, sourcePluginId: { type: String, default: '' }, navKey: { type: String, default: 'settings' }, embedded: { type: Boolean, default: false } })
const emit = defineEmits(['close'])
const initialConfig = ref(null)
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const id = computed(() => props.pluginId || PLUGIN_ID)

function goBack(saved = false) {
  if (props.embedded) { emit('close', saved === true); return }
  window.location.hash = `/plugin-app/${encodeURIComponent(id.value)}/main`
}
async function load() {
  if (loading.value) return
  loading.value = true; error.value = ''
  try {
    initialConfig.value = unpackPayload(await props.api.get(`plugin/${id.value}`, { feedback: 'silent' }))
  } catch { error.value = '设置暂时无法读取，请重试。' }
  finally { loading.value = false }
}
async function save(config) {
  if (saving.value) return
  saving.value = true; error.value = ''
  try {
    unpackPayload(await props.api.put(`plugin/${id.value}`, cloneConfig(config), { feedback: 'silent' }))
    goBack(true)
  } catch { error.value = '保存失败，输入内容已保留，请稍后重试。' }
  finally { saving.value = false }
}
onMounted(load)
</script>

<template>
  <main class="mp-settings-page pt-invite-page" :aria-busy="loading || saving">
    <div v-if="error" class="settings-feedback" role="alert"><span>{{ error }}</span><button v-if="!initialConfig" type="button" @click="load">重新加载</button></div>
    <div v-if="loading && !initialConfig" class="settings-loading" role="status"><span class="loading-dot"></span>正在读取设置…</div>
    <div v-if="initialConfig" :class="['settings-content', { 'is-saving': saving }]">
      <Config :initial-config="initialConfig" :api="api" :plugin-id="id" :source-plugin-id="sourcePluginId" @save="save" @close="goBack" @switch="goBack" />
      <div v-if="saving" class="saving-indicator" role="status">正在保存设置…</div>
    </div>
  </main>
</template>

<style scoped>
.mp-settings-page{box-sizing:border-box;width:100%;margin:0 auto;padding:16px 16px 0;color:rgb(var(--v-theme-on-surface,35,38,49));font-family:inherit}.settings-feedback{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 16px;margin-bottom:14px;border:1px solid rgba(202,113,73,.25);border-radius:10px;background:rgba(202,113,73,.08);color:#b46e38;font-size:13px}.settings-feedback button{border:0;background:transparent;color:inherit;font:inherit;font-weight:600;cursor:pointer}.settings-loading{display:flex;justify-content:center;align-items:center;gap:9px;min-height:180px;color:rgba(var(--v-theme-on-surface,35,38,49),.55);font-size:13px}.loading-dot{height:7px;width:7px;border-radius:50%;background:rgb(var(--v-theme-primary));animation:pulse 1s ease-in-out infinite alternate}.settings-content{position:relative;min-width:0}.settings-content.is-saving{pointer-events:none}.saving-indicator{position:absolute;inset:0;z-index:5;background:rgba(var(--v-theme-surface,255,255,255),.65);display:flex;justify-content:center;align-items:center;font-size:13px;font-weight:600;backdrop-filter:blur(1px)}@keyframes pulse{to{opacity:.3}}@media(prefers-reduced-motion:reduce){.loading-dot{animation:none}}@media(max-width:959px){.mp-settings-page{padding:12px 12px 0}}
</style>

<style src="./host-layout.css"></style>
