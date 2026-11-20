<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { loadSiteIcon } from './site-icons.mjs'

const props = defineProps({ api: Object, siteId: [String, Number], name: { type: String, default: '' } })
const element = ref(null), source = ref(''), failed = ref(false)
let observer, visible = false, generation = 0
async function load() {
  const current = ++generation
  const icon = await loadSiteIcon(props.api, props.siteId)
  if (current === generation) source.value = icon
}
watch(() => props.siteId, () => {
  generation++; source.value = ''; failed.value = false
  if (visible) load()
})
onMounted(() => {
  if (typeof IntersectionObserver === 'undefined') { visible = true; load(); return }
  observer = new IntersectionObserver(entries => {
    if (!entries.some(entry => entry.isIntersecting)) return
    visible = true; observer.disconnect(); load()
  }, { rootMargin: '80px' })
  observer.observe(element.value)
})
onBeforeUnmount(() => { generation++; observer?.disconnect() })
</script>

<template>
  <span ref="element" class="site-avatar" aria-hidden="true">
    <img v-if="source && !failed" :src="source" alt="" width="30" height="30" decoding="async" loading="lazy" @error="failed = true">
    <span v-else>{{ name.slice(0, 1) }}</span>
  </span>
</template>

<style scoped>
.site-avatar { display: grid; place-items: center; flex-shrink: 0; width: 32px; height: 32px; border-radius: 8px; color: var(--muted); font-size: 12px; font-weight: 600; }
.site-avatar img { display: block; width: 30px; height: 30px; object-fit: contain; border-radius: 4px; }
</style>
