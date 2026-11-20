import { readFileSync, readdirSync } from 'node:fs'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
for (const name of readdirSync('src').filter(name => name.endsWith('.vue'))) {
 const filename = `src/${name}`
 const { descriptor, errors } = parse(readFileSync(filename, 'utf8'), { filename })
 if (errors.length) throw errors[0]
 const script = compileScript(descriptor, { id: name })
 const template = compileTemplate({ source: descriptor.template.content, filename, id: name, compilerOptions: { bindingMetadata: script.bindings } })
 if (template.errors.length) throw template.errors[0]
 console.log(`${filename}: component syntax OK`)
}
