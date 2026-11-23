<script setup>
import './controls.css'
import { computed, ref, watch, getCurrentInstance } from 'vue'
import { selection, isAll, toggleSelection, selectVisible } from './config-model.mjs'
import KeeperIcon from './KeeperIcon.vue'
const props = defineProps({ modelValue: { default: () => [] }, options: { type: Array, default: () => [] }, label: { type: String, default: '选择站点' }, allowAll: { type: Boolean, default: true }, emptyMeansAll: { type: Boolean, default: false }, loading: Boolean })
const emit = defineEmits(['update:modelValue'])
const query = ref(''), selectedOnly = ref(false), page = ref(1), modeOverride = ref(null)
const group = `sites-${getCurrentInstance().uid}`
const values = computed(() => selection(props.modelValue))
const all = computed(() => props.allowAll && (modeOverride.value ?? isAll(values.value, props.emptyMeansAll)))
const selectedKeys = computed(() => new Set(values.value.map(String)))
const knownKeys = computed(() => new Set(props.options.map(option => String(option.id))))
const missing = computed(() => values.value.filter(value => String(value).toLowerCase() !== 'all' && !knownKeys.value.has(String(value))))
const filtered = computed(() => { const term = query.value.trim().toLocaleLowerCase(); return props.options.filter(option => String(option.name).toLocaleLowerCase().includes(term) && (!selectedOnly.value || selectedKeys.value.has(String(option.id)))) })
const pages = computed(() => Math.max(1, Math.ceil(filtered.value.length / 24)))
const visible = computed(() => filtered.value.slice((page.value - 1) * 24, page.value * 24))
const countLabel = computed(() => all.value ? `全部 ${props.options.length} 站` : props.emptyMeansAll && !values.value.length ? '留空 · 保存后管理全部' : `已选 ${values.value.length} 站`)
watch([query, selectedOnly], () => { page.value = 1 })
watch(pages, count => { page.value = Math.min(page.value, count) })
watch(() => props.modelValue, value => { if (isAll(value, false)) modeOverride.value = null })
function mode(next) { modeOverride.value = next === 'all'; emit('update:modelValue', next === 'all' ? ['all'] : isAll(values.value, props.emptyMeansAll) ? props.options.map(option => option.id) : values.value) }
function toggle(option, checked) { emit('update:modelValue', toggleSelection(values.value, option.id, checked)) }
</script>
<template>
  <section class="site-picker keeper-ui" :aria-label="label">
    <div class="picker-heading"><h4>{{ label }}</h4><span class="selection-count">{{ countLabel }}</span></div>
    <div v-if="allowAll" class="picker-modes" role="group" :aria-label="`${label}范围`"><label :class="{ chosen: all }"><input :name="group" type="radio" :checked="all" @change="mode('all')">全部站点</label><label :class="{ chosen: !all }"><input :name="group" type="radio" :checked="!all" @change="mode('custom')">指定站点</label></div>
    <p v-if="all" class="picker-hint">自动纳入当前及以后新增的站点。</p>
    <template v-else>
      <div class="keeper-search picker-search"><KeeperIcon name="search" :size="16" /><input v-model="query" class="keeper-control" type="search" :placeholder="`搜索${label}`" :aria-label="`搜索${label}`"><button v-if="query" type="button" class="search-clear" :aria-label="`清除${label}搜索`" @click="query = ''"><KeeperIcon name="close" :size="14" /></button></div>
      <div class="picker-toolbar"><label><input v-model="selectedOnly" type="checkbox">只看已选</label><div><button type="button" :disabled="!filtered.length || loading" @click="emit('update:modelValue', selectVisible(values, filtered, true))">{{ query ? '全选结果' : '全选' }}</button><button type="button" :disabled="!filtered.length || loading" @click="emit('update:modelValue', selectVisible(values, filtered, false))">{{ query ? '清除结果' : '清空' }}</button></div></div>
      <div v-if="loading" class="picker-empty" role="status">正在加载站点…</div>
      <div v-else-if="!filtered.length" class="picker-empty">{{ query || selectedOnly ? '没有匹配的站点' : '暂无站点选项，已保存的选择仍会保留' }}</div>
      <div v-else class="picker-options"><label v-for="option in visible" :key="String(option.id)" :title="option.name" :class="{ selected: selectedKeys.has(String(option.id)) }"><input type="checkbox" :checked="selectedKeys.has(String(option.id))" @change="toggle(option, $event.target.checked)"><span>{{ option.name }}</span></label></div>
      <div v-if="pages > 1" class="picker-pagination"><span>{{ filtered.length }} 站 · {{ page }} / {{ pages }}</span><div><button type="button" :disabled="page <= 1" :aria-label="`${label}上一页`" @click="page--">上一页</button><button type="button" :disabled="page >= pages" :aria-label="`${label}下一页`" @click="page++">下一页</button></div></div>
      <p v-if="query && filtered.length" class="picker-hint">批量操作适用于全部 {{ filtered.length }} 条搜索结果。</p>
      <p v-if="emptyMeansAll && !values.length" class="picker-hint">该插件留空表示管理全部站点。</p>
      <div v-if="missing.length" class="saved-options"><span>已保留旧选项</span><button v-for="item in missing" :key="String(item)" type="button" :aria-label="`移除已保存选项 ${item}`" @click="emit('update:modelValue', toggleSelection(values, item, false))">{{ item }} ×</button></div>
    </template>
  </section>
</template>
<style scoped>
.site-picker{border:1px solid var(--k-border);border-radius:11px;padding:15px;min-width:0;background:var(--k-surface)}.picker-heading{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:13px}.picker-heading h4{font-size:13px;font-weight:600;margin:0}.selection-count{font-size:11px;white-space:nowrap;color:var(--k-accent);font-variant-numeric:tabular-nums}.picker-modes{display:flex;gap:8px;flex-wrap:wrap}.picker-modes label{display:flex;align-items:center;gap:7px;padding:7px 10px;font-size:12px;border:1px solid var(--k-border);border-radius:7px;cursor:pointer}.picker-modes label.chosen{color:var(--k-accent);border-color:color-mix(in srgb,var(--k-accent) 40%,transparent);background:var(--k-tint)}.picker-hint{margin:10px 0 0;font-size:11px;color:var(--k-muted);line-height:1.65}.picker-search{margin-top:13px}.picker-toolbar{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:10px 0;font-size:11px;color:var(--k-muted)}.picker-toolbar label{display:flex;align-items:center;gap:6px;cursor:pointer}.picker-toolbar>div{display:flex;gap:9px}.picker-toolbar button,.picker-pagination button{border:0;background:transparent;color:var(--k-accent);border-radius:5px;padding:5px 2px;font:inherit;font-size:11px;cursor:pointer}.picker-toolbar button:disabled,.picker-pagination button:disabled{opacity:.4;cursor:default}.picker-options{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:3px 5px;max-height:245px;overflow:auto;scrollbar-width:thin;overscroll-behavior:contain;padding:3px}.picker-options label{display:flex;gap:8px;align-items:center;min-width:0;padding:8px 6px;font-size:12px;border-radius:6px;cursor:pointer}.picker-options label.selected{background:var(--k-tint)}.picker-options label:hover{background:var(--k-soft)}.picker-options span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.picker-empty{padding:24px 8px;text-align:center;font-size:12px;color:var(--k-muted)}.picker-pagination{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:9px;padding-top:9px;border-top:1px solid var(--k-border);font-size:10px;color:var(--k-muted)}.picker-pagination>div{display:flex;gap:10px}.saved-options{display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-top:12px;font-size:11px;color:var(--k-muted)}.saved-options button{border:1px solid var(--k-border);border-radius:5px;background:var(--k-soft);color:inherit;max-width:100%;overflow-wrap:anywhere;font:inherit;padding:3px 6px;cursor:pointer}
</style>
