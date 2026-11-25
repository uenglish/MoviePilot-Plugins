import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import federation from '@originjs/vite-plugin-federation'

export default defineConfig({
  base: './',
  // Keep identifier names stable across macOS/Linux build paths.
  esbuild: { minifyIdentifiers: false },
  plugins: [vue(), federation({
    name: "PTSignInKeeper",
    filename: 'remoteEntry.js',
    exposes: {
      "./Page": "./src/Page.vue",
      "./AppPage": "./src/AppPage.vue",
      "./AppPageSettings": "./src/SettingsPage.vue",
      "./Config": "./src/Config.vue",
      "./Dashboard": "./src/Dashboard.vue"
},
    shared: { vue: { requiredVersion: false, generate: false, singleton: true } },
    format: 'esm',
  })],
  build: {
    target: 'esnext', outDir: '../dist/assets', assetsDir: '', emptyOutDir: true,
    minify: 'esbuild', cssCodeSplit: true,
    rollupOptions: { input: './src/entry.js' },
  },
  css: { postcss: { plugins: [{
    postcssPlugin: 'exclude-host-global-styles',
    Root(root) {
      const file = root.source?.input?.file?.replaceAll('\\', '/') || ''
      if (file.includes('/node_modules/vuetify/') || file.includes('/node_modules/@mdi/')) root.removeAll()
    },
  }] } },
})
