<script setup>
import { computed, ref, getCurrentInstance } from 'vue'
import { selection, isAll, toggleSelection, selectVisible } from './config-model.mjs'

const props = defineProps({
  modelValue: { default: () => [] }, options: { type: Array, default: () => [] },
  label: { type: String, default: '选择站点' }, allowAll: { type: Boolean, default: true },
  emptyMeansAll: { type: Boolean, default: false }, loading: Boolean,
})
const emit = defineEmits(['update:modelValue'])
const query = ref('')
const modeOverride = ref(null)
const group = `sites-${getCurrentInstance().uid}`
const values = computed(() => selection(props.modelValue))
const all = computed(() => props.allowAll && (modeOverride.value ?? isAll(values.value, props.emptyMeansAll)))
const selectedKeys = computed(() => new Set(values.value.map(String)))
const knownKeys = computed(() => new Set(props.options.map(option => String(option.id))))
const missing = computed(() => values.value.filter(value => String(value).toLowerCase() !== 'all' && !knownKeys.value.has(String(value))))
const filtered = computed(() => props.options.filter(option => String(option.name).toLocaleLowerCase().includes(query.value.trim().toLocaleLowerCase())))
const countLabel = computed(() => all.value ? `全部 ${props.options.length} 站` : `已选 ${values.value.length} 站`)

function mode(next) {
  modeOverride.value = next === 'all'
  emit('update:modelValue', next === 'all' ? ['all'] : isAll(values.value, props.emptyMeansAll) ? props.options.map(option => option.id) : values.value)
}
function toggle(option, checked) {
  emit('update:modelValue', toggleSelection(values.value, option.id, checked))
}
</script>

<template>
  <section class="site-picker">
    <div class="picker-heading"><h4>{{ label }}</h4><span class="selection-count">{{ countLabel }}</span></div>
    <div v-if="allowAll" class="picker-modes" role="group" :aria-label="label">
      <label :class="{ chosen: all }"><input :name="group" type="radio" :checked="all" @change="mode('all')">全部站点<span>自动包含新增</span></label>
      <label :class="{ chosen: !all }"><input :name="group" type="radio" :checked="!all" @change="mode('custom')">指定站点</label>
    </div>
    <p v-if="all" class="picker-hint">当前及后续新增的站点都会自动纳入。</p>
    <template v-else>
      <div class="picker-toolbar">
        <label class="search"><span aria-hidden="true">⌕</span><input v-model="query" type="search" :placeholder="`搜索${label}`" :aria-label="`搜索${label}`"></label>
        <button type="button" @click="emit('update:modelValue', selectVisible(values, filtered, true))">选中结果</button>
        <button type="button" @click="emit('update:modelValue', selectVisible(values, filtered, false))">取消结果</button>
      </div>
      <div v-if="loading" class="picker-empty">正在加载站点…</div>
      <div v-else-if="!filtered.length" class="picker-empty">{{ query ? '没有匹配的站点' : '暂无站点选项，已保存的选择仍会保留' }}</div>
      <div v-else class="picker-options">
        <label v-for="option in filtered" :key="String(option.id)" :title="option.name" :class="{ selected: selectedKeys.has(String(option.id)) }">
          <input type="checkbox" :checked="selectedKeys.has(String(option.id))" @change="toggle(option, $event.target.checked)"><span>{{ option.name }}</span>
        </label>
      </div>
      <p v-if="emptyMeansAll && !values.length" class="picker-hint">按原设置逻辑，留空会管理全部站点。</p>
      <div v-if="missing.length" class="saved-options"><span>保留的旧选项</span><button v-for="item in missing" :key="String(item)" type="button" :aria-label="`移除已保存选项 ${item}`" @click="emit('update:modelValue', toggleSelection(values, item, false))">{{ item }} ×</button></div>
    </template>
  </section>
</template>

<style scoped>
.site-picker{border:1px solid var(--mc-border);border-radius:var(--app-control-radius,10px);padding:14px;background:transparent;min-width:0}.picker-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:11px}.picker-heading h4{font-size:13px;font-weight:650;margin:0}.selection-count{color:var(--mc-muted);font-size:12px;white-space:nowrap}.picker-modes{display:flex;flex-wrap:wrap;gap:8px}.picker-modes label{display:flex;align-items:center;gap:7px;border:1px solid var(--mc-border);border-radius:var(--app-control-radius,8px);padding:7px 10px;font-size:12px;cursor:pointer}.picker-modes label.chosen{border-color:var(--mc-accent);background:var(--mc-tint)}.picker-modes label span{color:var(--mc-muted);font-size:11px}.site-picker input[type=checkbox],.site-picker input[type=radio]{accent-color:var(--mc-accent);flex-shrink:0;width:14px;height:14px}.picker-hint{font-size:12px;color:var(--mc-muted);margin:10px 0 0;line-height:1.55}.picker-toolbar{display:flex;align-items:center;gap:7px;margin:12px 0 9px}.search{display:flex;gap:7px;align-items:center;border:1px solid var(--mc-border);border-radius:7px;min-width:0;flex:1;padding:0 8px;color:var(--mc-muted)}.search input{width:100%;min-width:0;border:0;outline:none;background:transparent;color:inherit;font:inherit;font-size:12px;min-height:31px}.picker-toolbar button{color:var(--mc-accent);border:0;background:transparent;font-size:11px;white-space:nowrap;padding:5px 2px;cursor:pointer}.picker-options{display:grid;grid-template-columns:repeat(auto-fill,minmax(135px,1fr));gap:3px 6px;max-height:210px;overflow:auto;overscroll-behavior:contain}.picker-options label{display:flex;align-items:center;gap:7px;min-width:0;padding:7px;border-radius:6px;font-size:12px;cursor:pointer}.picker-options label.selected{background:var(--mc-tint)}.picker-options label:hover{background:var(--mc-soft)}.picker-options span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.picker-empty{padding:16px;text-align:center;font-size:12px;color:var(--mc-muted)}.saved-options{display:flex;flex-wrap:wrap;align-items:center;gap:5px;margin-top:10px;font-size:11px;color:var(--mc-muted)}.saved-options button{border:1px solid var(--mc-border);border-radius:5px;background:var(--mc-soft);color:inherit;padding:2px 6px;cursor:pointer}.site-picker button:focus-visible,.site-picker input:focus-visible{outline:2px solid var(--mc-accent);outline-offset:2px}@media(max-width:520px){.picker-toolbar{flex-wrap:wrap}.search{flex-basis:100%}.picker-options{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>
