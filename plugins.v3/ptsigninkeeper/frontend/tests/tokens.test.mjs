import test from 'node:test'
import assert from 'node:assert/strict'
import { makeConfig, prepareConfig, browserlessTokens } from '../src/config-model.mjs'

test('legacy token fields populate the editable list without modifying the source', () => {
  const original = { browserless_token: ' one ', browserless_token3: 'two', browserless_token4: 'one' }
  const config = makeConfig(original)
  assert.deepEqual(config.browserless_tokens, ['one', 'two'])
  assert.equal(original.browserless_tokens, undefined)
})

test('adding more than four and removing middle entries survive save and reload', () => {
  const config = makeConfig({ browserless_token: 'old', future_option: { keep: true } })
  config.browserless_tokens.push(...Array.from({ length: 6 }, (_, i) => `fixture-${i}`))
  config.browserless_tokens.splice(2, 1)
  config.browserless_tokens.push(' ', ' fixture-5 ')
  const saved = prepareConfig(config)
  assert.deepEqual(saved.browserless_tokens, ['old', 'fixture-0', 'fixture-2', 'fixture-3', 'fixture-4', 'fixture-5'])
  assert.equal('browserless_token' in saved, false)
  assert.deepEqual(saved.future_option, { keep: true })
  assert.deepEqual(makeConfig(saved).browserless_tokens, saved.browserless_tokens)
})

test('deleting all remains empty despite legacy values and multiline fallback works', () => {
  const config = makeConfig({ browserless_token: 'old' })
  config.browserless_tokens.splice(0)
  assert.deepEqual(makeConfig(prepareConfig(config)).browserless_tokens, [])
  assert.deepEqual(browserlessTokens({ browserless_tokens: 'one\ntwo\none' }), ['one', 'two'])
})
