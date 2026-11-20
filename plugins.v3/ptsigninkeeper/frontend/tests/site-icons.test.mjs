import { test } from 'node:test'
import assert from 'node:assert/strict'
import { iconSource, loadSiteIcon } from '../src/site-icons.mjs'

test('native icons share pending requests and use the authenticated host API', async () => {
  let calls = 0
  const api = { get: async (path, options) => {
    calls++; assert.equal(path, 'site/icon/12'); assert.equal(options.feedback, 'silent')
    return { success: true, data: { icon: 'data:image/ico;base64,aGVsbG8=' } }
  } }
  const [first, second] = await Promise.all([loadSiteIcon(api, 12), loadSiteIcon(api, '12')])
  assert.equal(first, second); assert.ok(first.startsWith('data:image/ico')); assert.equal(calls, 1)
})
test('custom or missing site IDs do not request a native icon', async () => {
  const api = { get: () => { throw new Error('must not request') } }
  for (const id of [null, undefined, 'custom:1', '-1', 0]) assert.equal(await loadSiteIcon(api,id), '')
  assert.equal(await loadSiteIcon({get: async () => { throw new Error('missing') }},3),'')
})
test('only image data and ordinary image URLs are accepted', () => {
  assert.equal(iconSource('https://icons.example.test/site.png'),'https://icons.example.test/site.png')
  for (const value of ['javascript:alert(1)','data:text/html;base64,aGVsbG8=','https://secret:password@x.test/a',{}]) assert.equal(iconSource(value),'')
})
