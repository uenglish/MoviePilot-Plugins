import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createRequestGate, createDetailCache, readPreferences, savePreferences } from '../src/view-state.mjs'

test('filter changes and closing details invalidate in-flight responses before the next request', () => {
  const gate = createRequestGate(), first = gate.begin()
  gate.invalidate()
  assert.equal(gate.isCurrent(first), false)
  const second = gate.begin()
  assert.equal(gate.isCurrent(second), true)
  gate.invalidate()
  assert.equal(gate.isCurrent(second), false)
})
test('detail cache expires and clears after refresh without mixing member filters', () => {
  let time = 0
  const cache = createDetailCache(30, () => time)
  cache.set('a:all:1', { members: [] })
  assert.equal(cache.get('a:banned:1'), undefined)
  assert.deepEqual(cache.get('a:all:1'), { members: [] })
  time = 30
  assert.equal(cache.get('a:all:1'), undefined)
  cache.set('b', 0); assert.equal(cache.get('b'), 0)
  cache.clear(); assert.equal(cache.get('b'), undefined)
})
test('preferences handle invalid storage and never save unrelated fields', () => {
  let saved
  const storage = { getItem: () => '{invalid', setItem: (key, val) => { saved = [key, JSON.parse(val)] } }
  assert.deepEqual(readPreferences('x', storage), { compact: true, pageSize: 20 })
  savePreferences('x', { compact: false, pageSize: 1000, token: 'not-a-preference' }, storage)
  assert.deepEqual(saved, ['x', { compact: false, pageSize: 20 }])
})
