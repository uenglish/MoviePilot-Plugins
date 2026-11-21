import test from 'node:test'
import assert from 'node:assert/strict'
import { cloneConfig, makeConfig, selection, isAll, toggleSelection, selectVisible, unpackPayload, validateConfig } from '../src/config-model.mjs'

test('saving edited fields retains unknown options and nested credentials without mutating initialConfig', () => {
  const initial = { enabled: true, future_feature: { mode: 'keep' }, browserless_token: 'fixture-token', consolidation_version: 1 }
  const form = makeConfig(initial, 'signin')
  form.cron = '1 0 * * *'
  form.future_feature.mode = 'edited'
  assert.equal(initial.future_feature.mode, 'keep')
  const saved = cloneConfig(form)
  assert.equal(saved.browserless_token, 'fixture-token')
  assert.equal(saved.consolidation_version, 1)
  assert.equal(saved.onlyonce, false)
  assert.equal(saved.clean, false)
})

test('selectors recognise all marker and preserve inactive or custom IDs', () => {
  assert.equal(isAll(['ALL']), true)
  assert.equal(isAll([], true), true)
  assert.equal(isAll([]), false)
  assert.deepEqual(selection('[1,"custom"]'), [1, 'custom'])
  assert.deepEqual(toggleSelection([1, 'saved-site'], '1', false), ['saved-site'])
  assert.deepEqual(selectVisible(['saved-site'], [{ id: 2 }, { id: 3 }], true), ['saved-site', 2, 3])
})

test('filters remove only visible selection and do not discard hidden saved selections', () => {
  assert.deepEqual(selectVisible([1, 2, 'saved-site'], [{ id: 2 }], false), [1, 'saved-site'])
})

test('form validation catches invalid numeric settings and JSON without echoing secrets', () => {
  const config = makeConfig({ queue_cnt: 0, retry_time: '28:00', site_rules: '{"token":"private"', retry_count: 1.2 }, 'signin')
  const errors = validateConfig(config, 'signin')
  assert.ok(errors.queue_cnt && errors.retry_time && errors.site_rules && errors.retry_count)
  assert.equal(JSON.stringify(errors).includes('private'), false)
  assert.deepEqual(validateConfig(makeConfig({ cron: '2.3/9-23' }, 'signin'), 'signin'), {})
  assert.deepEqual(validateConfig(makeConfig({ cron: '0 */9 * * *' }, 'invitee'), 'invitee'), {})
})

test('api helper supports final raw and envelope contracts and preserves zero values', () => {
  assert.deepEqual(unpackPayload({ sites: [] }), { sites: [] })
  assert.deepEqual(unpackPayload({ success: true, message: '', data: { count: 0 } }), { count: 0 })
  assert.throws(() => unpackPayload({ success: false, message: '暂不可用', data: null }), /暂不可用/)
})


test('empty enabled or one-shot selection is rejected while login-only and disabled saves remain valid', () => {
  assert.ok(validateConfig(makeConfig({ enabled: true })).sign_sites)
  assert.ok(validateConfig(makeConfig({ onlyonce: true })).sign_sites)
  assert.deepEqual(validateConfig(makeConfig({ enabled: true, login_sites: [2] })), {})
  assert.deepEqual(validateConfig(makeConfig({ enabled: true, sign_sites: ['all'] })), {})
  assert.deepEqual(validateConfig(makeConfig({ enabled: false })), {})
})
