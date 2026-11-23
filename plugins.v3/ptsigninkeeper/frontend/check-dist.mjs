import assert from 'node:assert/strict'
import { readFile, stat } from 'node:fs/promises'

const entry = new URL('../dist/assets/remoteEntry.js', import.meta.url)
const manifest = await readFile(entry, 'utf8')
assert.ok(!manifest.includes('${__federation_expose_'), 'unresolved federation entry')
const chunks = [...manifest.matchAll(/["']([^"']+\.(?:js|css))["']/g)]
  .map(match => match[1]).filter(name => /^(?:\.\/)?[\w.-]+\.(?:js|css)$/.test(name))
assert.ok(chunks.length >= 4, 'expected page and settings chunks')
for (const name of new Set(chunks)) {
  assert.ok((await stat(new URL(name, entry))).isFile(), `missing remote asset: ${name}`)
}
