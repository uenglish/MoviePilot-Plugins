<script setup>
import './controls.css'
import { computed, nextTick, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import SitePicker from './SitePicker.vue'
import KeeperButton from './KeeperButton.vue'
import KeeperIcon from './KeeperIcon.vue'
import { PLUGIN_ID, PLUGIN_KIND, makeConfig, prepareConfig, unpackPayload, validateConfig } from './config-model.mjs'
import { createQuotaQueries, formatQuotaTime, formatUnits } from './quota-model.mjs'

const props = defineProps({ saving: Boolean, saveError: { type: String, default: '' }, initialConfig: { type: Object, default: () => ({}) }, api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: '' }, sourcePluginId: { type: String, default: '' } })
const emit = defineEmits(['save', 'close', 'switch', 'layout'])
const signin = PLUGIN_KIND === 'signin'
const config = ref(makeConfig(props.initialConfig))
const errors = ref({})
const options = ref([])
const quizAi = ref(null)
const richNotificationsSupported = ref(true)
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

const quotaRevision = ref(0)
const quotas = createQuotaQueries(payload => {
  if (!props.api?.post) throw new Error('API unavailable')
  return props.api.post(`plugin/${id.value}/ui/browserless-quota`, payload, { feedback: 'silent' })
}, () => { quotaRevision.value += 1 })
const bulkQuotaLoading = computed(() => { quotaRevision.value; return quotas.bulkLoading })
const hasTokens = computed(() => config.value.browserless_tokens?.some(token => token.trim()))
function quotaState(index) {
  quotaRevision.value
  return quotas.state(config.value.browserless_tokens[index], config.value.browserless_url)
}
watch(() => [config.value.browserless_url, ...(config.value.browserless_tokens || [])], () => quotas.reset(), { flush: 'sync' })
async function addToken() {
  config.value.browserless_tokens.push('')
  await nextTick()
  form.value?.querySelector(`[name="browserless_tokens-${config.value.browserless_tokens.length - 1}"]`)?.focus()
}

watch(() => props.initialConfig, value => { config.value = makeConfig(value); initialState.value = JSON.stringify(config.value); errors.value = {} })
async function loadOptions() {
  if (loading.value || disposed) return
  loading.value = true; loadError.value = ''
  try {
    if (!props.api?.get) throw new Error('连接尚未就绪，请重试')
    const result = unpackPayload(await props.api.get(`plugin/${id.value}/ui/options`, { feedback: 'silent' }))
    if (disposed) return
    options.value = Array.isArray(result.sites) ? result.sites : []
    quizAi.value = result.quiz_ai || null
    richNotificationsSupported.value = result.rich_notifications_supported !== false
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
  emit('save', prepareConfig(config.value))
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
            <label v-if="signin" class="field"><span>每日失败重试时间</span><input class="keeper-control" v-model="config.retry_time" name="retry_time" type="text" placeholder="09:01，留空关闭" :aria-invalid="Boolean(errors.retry_time)"><small class="field-help">补跑当天未完成的站点。若要每天 09:01 执行主任务，在执行周期填写 1 9 * * *。</small><small v-if="errors.retry_time" class="field-error">{{ errors.retry_time }}</small></label>
            <label v-if="signin" class="field"><span>并发站点数</span><input class="keeper-control" v-model.number="config.queue_cnt" name="queue_cnt" type="number" min="1" max="20" :aria-invalid="Boolean(errors.queue_cnt)"><small class="field-help">同时处理的站点数量，通常 3–5 个即可。</small><small v-if="errors.queue_cnt" class="field-error">{{ errors.queue_cnt }}</small></label>
            <label v-if="signin" class="field"><span>HTTP 超时 <small>秒</small></span><input class="keeper-control" v-model.number="config.timeout" name="timeout" type="number" min="5" max="120" :aria-invalid="Boolean(errors.timeout)"><small class="field-help">单次网络请求等待时间。</small><small v-if="errors.timeout" class="field-error">{{ errors.timeout }}</small></label>
          </div>
          <div class="inline-options">
            <label><input v-model="config.onlyonce" type="checkbox"><span>保存后执行一次</span></label>
            <label v-if="signin && richNotificationsSupported"><input v-model="config.rich_notify" type="checkbox"><span>富文本通知</span></label>
            <label v-if="signin"><input v-model="config.read_activity" type="checkbox"><span>读取最近动向</span></label>
          </div>
          <p v-if="signin && !richNotificationsSupported" class="section-note">通知按摘要、异常和成功结果分段显示。</p>
          <div v-if="config.onlyonce" class="action-note">保存设置后会{{ signin ? '执行一次所选站点的签到与登录任务' : '刷新所选站点的邀请数据' }}，任务加入队列后此选项自动关闭。</div>
        </div>
      </details>

      <details class="config-section" open data-section="sites">
        <summary @click="activeSection = 'sites'"><span class="section-index">02</span><span><strong>站点范围</strong><small>全部自动纳入，或选择指定站点</small></span><span class="chevron">⌄</span></summary>
        <div class="section-content">
          <div v-if="loadError" class="load-error" role="status">{{ loadError }}<button type="button" @click="loadOptions">重新加载</button></div>
          <div v-if="signin" class="site-grid" name="sign_sites" tabindex="-1" :aria-invalid="Boolean(errors.sign_sites)">
            <SitePicker v-model="config.sign_sites" label="签到站点" :options="options" :loading="loading" />
            <SitePicker v-model="config.login_sites" label="登录站点" :options="options" :loading="loading" />
          </div>
          <SitePicker v-else v-model="config.site_ids" label="管理站点" :options="options" :loading="loading" empty-means-all />
          <p v-if="errors.sign_sites" class="field-error" role="alert">{{ errors.sign_sites }}</p>
          <p v-if="signin" class="section-note">至少选择一个签到或登录站点；两项都留空时不执行任务。同站点的签到与登录共用已确认结果。</p>
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
        <summary @click="activeSection = 'browser'"><span class="section-index">04</span><span><strong>浏览器服务</strong><small>Browserless、站点白名单与账户额度</small></span><span class="chevron">⌄</span></summary>
        <div class="section-content">
          <div class="field-grid">
            <label class="field"><span>Browserless 服务地址</span><input class="keeper-control" v-model="config.browserless_url" name="browserless_url" type="text" spellcheck="false" placeholder="https://…"><small class="field-help">用于需要浏览器验证的站点。</small></label>
            <label class="field"><span>浏览器超时 <small>秒</small></span><input class="keeper-control" v-model.number="config.browserless_timeout" name="browserless_timeout" type="number" min="15" max="300" :aria-invalid="Boolean(errors.browserless_timeout)"><small v-if="errors.browserless_timeout" class="field-error">{{ errors.browserless_timeout }}</small></label>
          </div>
          <div class="browserless-whitelist">
            <SitePicker v-model="config.browserless_sites" label="Browserless 站点白名单" :options="options" :allow-all="false" :loading="loading" />
            <div class="whitelist-help"><p class="section-note">仅允许所选站点调用 Browserless，包含自动回退；留空全部禁止。HTTP、本机浏览器不受影响，新增站点需手动勾选。</p><button type="button" class="text-button" :disabled="!config.browserless_sites.length" @click="config.browserless_sites = []">清空白名单</button></div>
          </div>
          <div class="subsection-header"><strong>访问令牌 · {{ config.browserless_tokens.length }} 个</strong><div class="token-header-actions"><button type="button" class="text-button" :disabled="!hasTokens || bulkQuotaLoading" @click="quotas.queryAll(config.browserless_tokens, config.browserless_url)">{{ bulkQuotaLoading ? '查询中…' : '查询全部额度' }}</button><button type="button" class="text-button" :aria-pressed="revealTokens" @click="revealTokens = !revealTokens">{{ revealTokens ? '隐藏令牌' : '显示令牌' }}</button></div></div>
          <div class="token-list">
            <div v-for="(_, index) in config.browserless_tokens" :key="index" class="token-item">
              <div class="token-row"><label class="field"><span>Token {{ index + 1 }}</span><input class="keeper-control" v-model="config.browserless_tokens[index]" :name="`browserless_tokens-${index}`" :type="revealTokens ? 'text' : 'password'" autocomplete="off" spellcheck="false" placeholder="填写 Token"></label><button type="button" class="text-button token-query" :disabled="!config.browserless_tokens[index].trim() || quotaState(index).loading || bulkQuotaLoading" :aria-label="`查询 Token ${index + 1} 额度`" @click="quotas.query(config.browserless_tokens[index], config.browserless_url)">{{ quotaState(index).loading ? '查询中…' : '查额度' }}</button><KeeperButton type="button" variant="text" color="error" icon="mdi-delete-outline" :aria-label="`删除 Token ${index + 1}`" @click="config.browserless_tokens.splice(index, 1)" /></div>
              <div v-if="quotaState(index).loading || quotaState(index).status" class="quota-result" role="status" aria-live="polite" :aria-label="`Token ${index + 1} 账户额度`">
                <span v-if="quotaState(index).loading">正在查询账户额度…</span>
                <template v-else-if="quotaState(index).status === 'ok'">
                  <div class="quota-heading"><strong>{{ quotaState(index).plan || '账户额度' }}</strong><span>所属账户 · units</span></div>
                  <div v-if="quotaState(index).unit_based" class="quota-values">
                    <span><small>剩余</small><strong :class="{ 'quota-empty': quotaState(index).remaining != null && quotaState(index).remaining <= 0 }">{{ formatUnits(quotaState(index).remaining) }}</strong></span>
                    <span><small>本期已用</small><strong>{{ formatUnits(quotaState(index).used) }}</strong></span>
                    <span><small>套餐额度</small><strong>{{ formatUnits(quotaState(index).limit) }}</strong></span>
                  </div>
                  <p v-else>该账户未提供按 units 计算的额度，请在 Browserless 控制台查看。</p>
                  <p>本期结束：{{ formatQuotaTime(quotaState(index).period_end, true) }}</p>
                  <p>查询于 {{ formatQuotaTime(quotaState(index).checked_at) }}<template v-if="quotaState(index).cached"> · 1 分钟内缓存</template></p>
                </template>
                <span v-else class="quota-error">{{ quotaState(index).message }}</span>
              </div>
            </div>
          </div>
          <p v-if="!config.browserless_tokens.length" class="section-note">尚未添加 Token。</p>
          <KeeperButton type="button" class="token-add" variant="tonal" color="primary" size="small" prepend-icon="mdi-plus" @click="addToken">添加 Token</KeeperButton>
          <p class="section-note">可自由增删，数量不限；保存时忽略空项和重复项，按列表顺序轮换使用。</p>
          <p class="section-note">额度查询支持未保存的 Token，仅在点击时读取官方账户数据，不启动浏览器；结果缓存 1 分钟。多个 Token 可能共享同一账户额度，不能相加。自建服务不支持此查询。</p>
          <details class="advanced-rules"><summary>自定义站点规则 <span>高级</span></summary><label class="field rules-field"><span>自定义站点规则 <small>JSON</small></span><textarea class="keeper-control" v-model="config.site_rules" name="site_rules" rows="6" spellcheck="false" placeholder='{"example.org": {}}' :aria-invalid="Boolean(errors.site_rules)"></textarea><small class="field-help">按站点域名或 ID 配置；留空使用内置规则。</small><small v-if="errors.site_rules" class="field-error">{{ errors.site_rules }}</small></label></details>
        </div>
      </details>

      <details v-if="!signin" class="config-section" data-section="preferences">
        <summary @click="activeSection = 'preferences'"><span class="section-index">03</span><span><strong>展示与访问偏好</strong><small>默认筛选、浏览器仿真范围</small></span><span class="chevron">⌄</span></summary>
        <div class="section-content">
          <label class="field"><span>默认站点搜索词</span><input class="keeper-control" v-model="config.page_filter" name="page_filter" type="search" placeholder="留空显示全部站点"><small class="field-help">打开详情页时按此关键词筛选，也可在页面临时调整。</small></label>
          <div class="browser-picker"><SitePicker v-model="config.no_browser_sites" label="不使用浏览器仿真的站点" :options="nameOptions" :allow-all="false" :loading="loading" /></div>
          <p class="section-note">这些站点遇到验证页时不启动浏览器仿真；其他访问设置仍按站点配置执行。</p>
        </div>
      </details>

      <details v-if="signin" class="config-section" data-section="quiz">
        <summary @click="activeSection = 'quiz'"><span class="section-index">05</span><span><strong>答题签到</strong><small>已知答案优先，AI 辅助识别</small></span><span class="chevron">⌄</span></summary>
        <div class="section-content">
          <label class="maintenance-choice"><input v-model="config.quiz_ai_enabled" type="checkbox"><span><strong>使用 MoviePilot 智能助手辅助答题</strong><small>彩虹岛使用文字题；U2 需要支持图像输入的模型。只发送题目、选项及必要题图。</small></span></label>
          <p class="section-note" role="status">{{ quizAi?.message || '请先在 MoviePilot 智能助手中配置并测试连接。' }}<template v-if="quizAi?.model"> · {{ quizAi.model }}</template></p>
          <p class="section-note">未配置可用 AI 时，U2 随机签到，彩虹岛使用“不会”保底。AI 可能答错；调用超时或答案不明确时保留待处理，同一天最多提交一次。</p>
          <label v-if="config.quiz_ai_enabled" class="field rules-field"><span>AI 等待时间 <small>秒</small></span><input class="keeper-control" v-model.number="config.quiz_ai_timeout" name="quiz_ai_timeout" type="number" min="10" max="120" :aria-invalid="Boolean(errors.quiz_ai_timeout)"><small v-if="errors.quiz_ai_timeout" class="field-error">{{ errors.quiz_ai_timeout }}</small></label>
        </div>
      </details>

      <details v-if="signin" class="config-section maintenance" data-section="maintenance">
        <summary @click="activeSection = 'maintenance'"><span class="section-index">06</span><span><strong>记录维护</strong><small>重新执行前的缓存选项</small></span><span class="chevron">⌄</span></summary>
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
