import test from 'node:test'
import assert from 'node:assert/strict'
import { makeConfig, prepareConfig, browserlessSites, toggleSelection, selectVisible } from '../src/config-model.mjs'

test('missing, empty, wildcard and malformed whitelist never grant all sites', () => {
  for (const value of [undefined, null, [], '', 'all', ['all'], ['*'], true, { 1: true }]) {
    assert.deepEqual(makeConfig({ browserless_sites: value }).browserless_sites, [])
  }
  assert.deepEqual(browserlessSites(['1', 1, '002', ' custom-a ', 'custom-a', false, 0, -1, {}, 1.5]), [1, 2, 'custom-a'])
  assert.deepEqual(browserlessSites('[1,"custom-a"]'), [1, 'custom-a'])
})

test('editing and clearing whitelist survives save and reload without restoring old selections', () => {
  const original = { browserless_sites: [1, 'custom-a'], browserless_tokens: ['fixture-token'], future: { keep: true } }
  const config = makeConfig(original)
  config.browserless_sites = toggleSelection(config.browserless_sites, 2, true)
  config.browserless_sites = toggleSelection(config.browserless_sites, '1', false)
  assert.deepEqual(makeConfig(prepareConfig(config)).browserless_sites, ['custom-a', 2])
  config.browserless_sites = []
  const saved = prepareConfig(config)
  assert.deepEqual(makeConfig(saved).browserless_sites, [])
  assert.deepEqual(saved.browserless_tokens, original.browserless_tokens)
  assert.deepEqual(saved.future, original.future)
  assert.deepEqual(original.browserless_sites, [1, 'custom-a'])
})

test('selecting search results records explicit IDs and does not grant future sites', () => {
  const options = [{ id: 1 }, { id: 2 }]
  const values = selectVisible(['saved-site'], options, true)
  options.push({ id: 3 })
  const saved = prepareConfig(makeConfig({ browserless_sites: values }))
  assert.deepEqual(saved.browserless_sites, ['saved-site', 1, 2])
  assert.equal(saved.browserless_sites.includes(3), false)
})
