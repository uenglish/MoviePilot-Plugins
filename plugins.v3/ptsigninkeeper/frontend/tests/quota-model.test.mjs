import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createQuotaQueries, formatUnits, formatQuotaTime } from '../src/quota-model.mjs'

const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const ok = { status: 'ok', remaining: 962, used: 37, limit: 1000 }

test('zero, unknown and negative balances stay distinct', () => {
  assert.equal(formatUnits(0), '0')
  assert.equal(formatUnits(-2), '-2')
  assert.equal(formatUnits(null), '未知')
  assert.equal(formatUnits(false), '未知')
  assert.equal(formatUnits('1000'), '未知')
  assert.equal(formatUnits(1000), '1,000')
  assert.equal(formatQuotaTime(null, true), '未知')
  assert.equal(formatQuotaTime('not a date'), '未知')
})

test('lookup is explicit, uses draft input, and deduplicates concurrent queries', async () => {
  const pending = deferred()
  const calls = []
  const quotas = createQuotaQueries(payload => { calls.push(payload); return pending.promise })
  assert.equal(calls.length, 0)
  const first = quotas.query(' draft-token ', 'https://production-sfo.browserless.io')
  await quotas.query('draft-token', 'https://production-sfo.browserless.io')
  assert.equal(calls.length, 1)
  assert.deepEqual(calls[0], { token: 'draft-token', browserless_url: 'https://production-sfo.browserless.io' })
  pending.resolve(ok); await first
  assert.equal(quotas.state('draft-token', 'https://production-sfo.browserless.io').remaining, 962)
  assert.deepEqual(quotas.state('draft-token', 'http://localhost'), {})
})

test('editing or deleting a token discards in-flight results', async () => {
  const pending = deferred()
  const quotas = createQuotaQueries(() => pending.promise)
  const run = quotas.query('old', '')
  quotas.reset()
  pending.resolve(ok); await run
  assert.deepEqual(quotas.state('old', ''), {})
  assert.deepEqual(quotas.state('new', ''), {})
})

test('bulk lookup limits concurrency and ignores blank or duplicate tokens', async () => {
  const pending = []
  const calls = []
  const quotas = createQuotaQueries(payload => { calls.push(payload.token); const item = deferred(); pending.push(item); return item.promise })
  const all = quotas.queryAll(['a', ' b ', 'a', '', 'c', 'd'], '')
  assert.equal(quotas.bulkLoading, true)
  assert.deepEqual(calls, ['a', 'b', 'c'])
  pending[0].resolve(ok)
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(calls, ['a', 'b', 'c', 'd'])
  pending.slice(1).forEach(item => item.resolve(ok)); await all
  assert.equal(quotas.bulkLoading, false)
})

test('changing settings cancels queued bulk lookups', async () => {
  const pending = deferred()
  let count = 0
  const quotas = createQuotaQueries(() => { count += 1; return pending.promise })
  const all = quotas.queryAll(['a', 'b', 'c', 'd', 'e'], '')
  quotas.reset(); pending.resolve(ok); await all
  assert.equal(count, 3)
  assert.equal(quotas.bulkLoading, false)
  assert.deepEqual(quotas.state('a', ''), {})
})

test('request errors never expose tokens; a retry can succeed', async () => {
  let fail = true
  const quotas = createQuotaQueries(() => { if (fail) throw new Error('secret-token'); return { success: true, data: ok } })
  await quotas.query('secret-token', '')
  assert.equal(quotas.state('secret-token', '').status, 'error')
  assert.ok(!JSON.stringify(quotas.state('secret-token', '')).includes('secret-token'))
  fail = false; await quotas.query('secret-token', '')
  assert.equal(quotas.state('secret-token', '').status, 'ok')
})
