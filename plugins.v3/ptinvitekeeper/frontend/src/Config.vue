<script setup>
import './controls.css'
import { computed, nextTick, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import SitePicker from './SitePicker.vue'
import KeeperButton from './KeeperButton.vue'
import KeeperIcon from './KeeperIcon.vue'
import { PLUGIN_ID, PLUGIN_KIND, makeConfig, cloneConfig, unpackPayload, validateConfig } from './config-model.mjs'

const props = defineProps({ saving: Boolean, saveError: { type: String, default: '' }, initialConfig: { type: Object, default: () => ({}) }, api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: '' }, sourcePluginId: { type: String, default: '' } })
const emit = defineEmits(['save', 'close', 'switch', 'layout'])
const signin = PLUGIN_KIND === 'signin'
const config = ref(makeConfig(props.initialConfig))
const errors = ref({})
const options = ref([])
const loading = ref(false)
const loadError = ref('')
const revealTokens = ref(false)
const form = ref(null)
const initialState = ref(JSON.stringify(config.value))
const dirty = computed(() => JSON.stringify(config.value) !== initialState.value)
const nameOptions = computed(() => options.value.map(site => ({ id: site.name, name: site.name })))
const id = computed(() => props.pluginId || PLUGIN_ID)
const title = signin ? '签到设置' : '邀请管理设置'
const activeSection = ref('schedule')
const confirmLeave = ref(false)
let disposed = false
const sections = computed(() => signin ? [
  { key: 'schedule', title: '执行计划', note: '时间与任务' },
  { key: 'sites', title: '站点范围', note: '签到与登录' },
  { key: 'retry', title: '失败处理', note: '重试与超时' },
  { key: 'browser', title: '浏览器服务', note: '白名单与额度' },
  { key: 'quiz', title: '答题签到', note: '智能助手' },
  { key: 'maintenance', title: '记录维护', note: '今日缓存' },
] : [
  { key: 'schedule', title: '执行计划', note: '时间与通知' },
  { key: 'sites', title: '站点范围', note: '邀请数据' },
  { key: 'preferences', title: '展示偏好', note: '默认筛选' },
])
async function jumpToSection(key) {
  activeSection.value = key
  const section = form.value?.querySelector(`[data-section="${key}"]`)
  if (section) { section.open = true; await nextTick(); section.scrollIntoView({ block: 'start', behavior: 'instant' }); section.querySelector('summary')?.focus({ preventScroll: true }) }
}
function toggleSections(open) { form.value?.querySelectorAll('details.config-section').forEach(item => { item.open = open }) }
async function requestClose() {
  if (props.saving) return
  if (!dirty.value) { emit('close'); return }
  confirmLeave.value = true
  await nextTick()
  form.value?.querySelector('.leave-keep')?.focus()
}
onBeforeUnmount(() => { disposed = true })

const tokenKeys = ['browserless_token', 'browserless_token2', 'browserless_token3', 'browserless_token4']

watch(() => props.initialConfig, value => { config.value = makeConfig(value); initialState.value = JSON.stringify(config.value); errors.value = {} })
async function loadOptions() {
  if (loading.value || disposed) return
  loading.value = true; loadError.value = ''
  try {
    if (!props.api?.get) throw new Error('连接尚未就绪，请重试')
    const result = unpackPayload(await props.api.get(`plugin/${id.value}/ui/options`, { feedback: 'silent' }))
    if (disposed) return
    options.value = Array.isArray(result.sites) ? result.sites : []
  } catch { loadError.value = '站点选项暂时无法加载；已有选择会原样保留。' }
  finally { loading.value = false }
}
async function save() {
  if (props.saving) return
  errors.value = validateConfig(config.value)
  if (Object.keys(errors.value).length) {
    await nextTick()
    const key = Object.keys(errors.value)[0]
    const field = form.value?.querySelector(`[name="${key}"]`)
    for (let parent = field?.parentElement; parent; parent = parent.parentElement) if (parent.tagName === 'DETAILS') parent.open = true
    const target = field?.matches('input,select,textarea') ? field : field?.querySelector('input,select,textarea')
    target?.focus()
    return
  }
  emit('save', cloneConfig(config.value))
}
onMounted(() => { emit('layout', { maxWidth: '72rem' }); loadOptions() })
</script>

<template>
  <section class="mp-config keeper-ui"><form ref="form" novalidate @submit.prevent="save">
    <header class="config-header">
      <div><div class="eyebrow">{{ signin ? 'PT 签到管家' : 'PT 邀请管家' }}</div><h2>{{ title }}</h2><p>调整执行计划、站点范围和{{ signin ? '访问策略' : '展示偏好' }}。</p></div>
      <KeeperButton type="button" icon="mdi-close" variant="text" :disabled="saving" aria-label="关闭设置" @click="requestClose" />
    </header>

    <div v-if="confirmLeave" class="leave-confirm" role="alert"><div><strong>还有未保存的更改</strong><p>可以继续编辑，或放弃更改返回。</p></div><div><KeeperButton class="leave-keep" variant="filled" @click="confirmLeave = false">继续编辑</KeeperButton><KeeperButton @click="emit('close')">放弃更改</KeeperButton></div></div>
    <fieldset class="config-layout" :disabled="saving">
      <aside class="config-nav"><div class="nav-label">设置目录</div><nav aria-label="设置分组"><button v-for="(section, index) in sections" :key="section.key" type="button" :aria-current="activeSection === section.key ? 'true' : undefined" :class="{ active: activeSection === section.key }" @click="jumpToSection(section.key)"><span class="nav-number">0{{ index + 1 }}</span><span><strong>{{ section.title }}</strong><small>{{ section.note }}</small></span></button></nav><div class="nav-actions"><button type="button" class="text-button" @click="toggleSections(true)">全部展开</button><button type="button" class="text-button" @click="toggleSections(false)">全部收起</button></div><p>高级选项按需展开，设置保存后生效。</p></aside>
      <div class="config-body">
      <div class="master-controls">
        <label class="toggle-card"><span><strong>启用插件</strong><small>按计划自动执行</small></span><input v-model="config.enabled" type="checkbox" role="switch"></label>
        <label class="toggle-card"><span><strong>发送通知</strong><small>{{ signin ? '推送签到与登录结果' : '推送邀请状态变化' }}</small></span><input v-model="config.notify" type="checkbox" role="switch"></label>
      </div>

      <details class="config-section" open data-section="schedule">
        <summary @click="activeSection = 'schedule'"><span class="section-index">01</span><span><strong>执行计划</strong><small>日程、重试与单次运行</small></span><span class="chevron">⌄</span></summary>
        <div class="section-content">
          <div class="field-grid">
            <label class="field"><span>执行周期 <small>Cron</small></span><input class="keeper-control" v-model="config.cron" name="cron" type="text" spellcheck="false" :placeholder="signin ? '1 0 * * *' : '0 9 * * *'" :aria-invalid="Boolean(errors.cron)"><small class="field-help">{{ signin ? '支持 5 段 Cron 或小时周期；留空于 9–23 点随机执行。' : '例如 0 9 * * *，每天 09:00 刷新邀请数据。' }}</small><small v-if="errors.cron" class="field-error">{{ errors.cron }}</small></label>
            <label v-if="signin" class="field"><span>每日失败重试时间</span><input class="keeper-control" v-model="config.retry_time" name="retry_time" type="text" placeholder="09:01，留空关闭" :aria-invalid="Boolean(errors.retry_time)"><small class="field-help">仅重试当天未完成的站点。</small><small v-if="errors.retry_time" class="field-error">{{ errors.retry_time }}</small></label>
            <label v-if="signin" class="field"><span>并发站点数</span><input class="keeper-control" v-model.number="config.queue_cnt" name="queue_cnt" type="number" min="1" max="20" :aria-invalid="Boolean(errors.queue_cnt)"><small class="field-help">同时处理的站点数量，通常 3–5 个即可。</small><small v-if="errors.queue_cnt" class="field-error">{{ errors.queue_cnt }}</small></label>
            <label v-if="signin" class="field"><span>HTTP 超时 <small>秒</small></span><input class="keeper-control" v-model.number="config.timeout" name="timeout" type="number" min="5" max="120" :aria-invalid="Boolean(errors.timeout)"><small class="field-help">单次网络请求等待时间。</small><small v-if="errors.timeout" class="field-error">{{ errors.timeout }}</small></label>
          </div>
          <div class="inline-options">
            <label><input v-model="config.onlyonce" type="checkbox"><span>保存后执行一次</span></label>
            <label v-if="signin"><input v-model="config.rich_notify" type="checkbox"><span>按结果分组通知</span></label>
            <label v-if="signin"><input v-model="config.read_activity" type="checkbox"><span>读取最近动向</span></label>
          </div>
          <div v-if="config.onlyonce" class="action-note">保存设置后会{{ signin ? '执行一次签到与登录任务' : '刷新所选站点的邀请数据' }}，完成后此选项自动关闭。</div>
        </div>
      </details>

      <details class="config-section" open data-section="sites">
        <summary @click="activeSection = 'sites'"><span class="section-index">02</span><span><strong>站点范围</strong><small>全部自动纳入，或选择指定站点</small></span><span class="chevron">⌄</span></summary>
        <div class="section-content">
          <div v-if="loadError" class="load-error" role="status">{{ loadError }}<button type="button" @click="loadOptions">重新加载</button></div>
          <div v-if="signin" class="site-grid">
            <SitePicker v-model="config.sign_sites" label="签到站点" :options="options" :loading="loading" />
            <SitePicker v-model="config.login_sites" label="登录站点" :options="options" :loading="loading" />
          </div>
          <SitePicker v-else v-model="config.site_ids" label="管理站点" :options="options" :loading="loading" empty-means-all />
          <p v-if="signin" class="section-note">同站点的签到与登录共用已确认结果，避免重复访问。</p>
        </div>
      </details>

      <details v-if="signin" class="config-section" data-section="retry">
        <summary @click="activeSection = 'retry'"><span class="section-index">03</span><span><strong>失败处理</strong><small>短间隔重试与网络优选</small></span><span class="chevron">⌄</span></summary>
        <div class="section-content field-grid">
          <label class="field"><span>额外重试次数</span><input class="keeper-control" v-model.number="config.retry_count" name="retry_count" type="number" min="0" max="5" :aria-invalid="Boolean(errors.retry_count)"><small class="field-help">0 关闭短间隔重试。</small><small v-if="errors.retry_count" class="field-error">{{ errors.retry_count }}</small></label>
          <label class="field"><span>重试间隔 <small>分钟</small></span><input class="keeper-control" v-model.number="config.retry_delay_minutes" name="retry_delay_minutes" type="number" min="1" max="1440" :aria-invalid="Boolean(errors.retry_delay_minutes)"><small v-if="errors.retry_delay_minutes" class="field-error">{{ errors.retry_delay_minutes }}</small></label>
          <label class="field"><span>重试关键词 <small>正则表达式</small></span><input class="keeper-control" v-model="config.retry_keyword" name="retry_keyword" type="text" placeholder="错误|失败" spellcheck="false"><small class="field-help">标记失败原因，已成功站点不会因关键词重复签到。</small></label>
          <label class="field"><span>Cloudflare 自动优选阈值</span><input class="keeper-control" v-model.number="config.auto_cf" name="auto_cf" type="number" min="0" max="1000" :aria-invalid="Boolean(errors.auto_cf)"><small class="field-help">0 关闭；失败命中数达到阈值时触发已配置的优选插件。</small><small v-if="errors.auto_cf" class="field-error">{{ errors.auto_cf }}</small></label>
        </div>
      </details>

      <details v-if="signin" class="config-section" data-section="browser">
        <summary @click="activeSection = 'browser'"><span class="section-index">04</span><span><strong>浏览器与站点规则</strong><small>Browserless、访问令牌与专用规则</small></span><span class="chevron">⌄</span></summary>
        <div class="section-content">
          <div class="field-grid">
            <label class="field"><span>Browserless 服务地址</span><input class="keeper-control" v-model="config.browserless_url" name="browserless_url" type="text" spellcheck="false" placeholder="https://…"><small class="field-help">用于需要浏览器验证的站点。</small></label>
            <label class="field"><span>浏览器超时 <small>秒</small></span><input class="keeper-control" v-model.number="config.browserless_timeout" name="browserless_timeout" type="number" min="15" max="300" :aria-invalid="Boolean(errors.browserless_timeout)"><small v-if="errors.browserless_timeout" class="field-error">{{ errors.browserless_timeout }}</small></label>
          </div>
          <div class="subsection-header"><strong>访问令牌</strong><button type="button" class="text-button" :aria-pressed="revealTokens" @click="revealTokens = !revealTokens">{{ revealTokens ? '隐藏令牌' : '显示令牌' }}</button></div>
          <div class="field-grid">
            <label v-for="(key, index) in tokenKeys" :key="key" class="field"><span>Token {{ index + 1 }} <small>{{ index ? '可选' : '' }}</small></span><input class="keeper-control" v-model="config[key]" :name="key" :type="revealTokens ? 'text' : 'password'" autocomplete="off" spellcheck="false" placeholder="未设置"></label>
          </div>
          <label class="field rules-field"><span>自定义站点规则 <small>JSON</small></span><textarea class="keeper-control" v-model="config.site_rules" name="site_rules" rows="6" spellcheck="false" placeholder='{"example.org": {}}' :aria-invalid="Boolean(errors.site_rules)"></textarea><small class="field-help">沿用 PT 站点签到规则格式；留空使用内置规则。</small><small v-if="errors.site_rules" class="field-error">{{ errors.site_rules }}</small></label>
        </div>
      </details>

      <details v-if="!signin" class="config-section" data-section="preferences">
        <summary @click="activeSection = 'preferences'"><span class="section-index">03</span><span><strong>展示与访问偏好</strong><small>默认筛选与站点访问</small></span><span class="chevron">⌄</span></summary>
        <div class="section-content">
          <label class="field"><span>默认站点搜索词</span><input class="keeper-control" v-model="config.page_filter" name="page_filter" type="search" placeholder="留空显示全部站点"><small class="field-help">打开详情页时按此关键词筛选，也可在页面临时调整。</small></label>
          <p class="section-note">站点要求人工验证时，请在站点内完成验证后刷新。</p>
        </div>
      </details>

      <details v-if="signin" class="config-section maintenance" data-section="maintenance">
        <summary @click="activeSection = 'maintenance'"><span class="section-index">05</span><span><strong>记录维护</strong><small>重新执行前的缓存选项</small></span><span class="chevron">⌄</span></summary>
        <div class="section-content"><label class="maintenance-choice"><input v-model="config.clean" type="checkbox"><span><strong>下次任务清理今日缓存</strong><small>下次执行会重新处理今日站点；历史记录不会受影响。</small></span></label></div>
      </details>
    </div>

    </fieldset>
    <footer class="config-footer">
      <span :class="['save-state', { changed: dirty, error: saveError }]" role="status"><i></i>{{ saveError || (dirty ? '有未保存的更改' : '设置已同步') }}</span>
      <div><KeeperButton type="button" variant="text" :disabled="saving" @click="requestClose">返回列表</KeeperButton><KeeperButton type="submit" color="primary" variant="filled" :loading="saving" prepend-icon="mdi-content-save-outline">保存设置</KeeperButton></div>
    </footer>
  </form></section>
</template>


<style scoped src="./config.css"></style>
