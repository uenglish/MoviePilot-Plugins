<script setup>
import { computed } from 'vue'
const props = defineProps({ name: { type: String, default: '' }, size: { type: [Number, String], default: 18 } })
const paths = {
  search: ['m21 21-4.4-4.4', 'M19 10.5a8.5 8.5 0 1 1-17 0 8.5 8.5 0 0 1 17 0'],
  settings: ['M4 7h16M4 17h16M8 4v6M16 14v6'],
  refresh: ['M20 7v5h-5M4 17v-5h5', 'M6.1 6.1A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.9 5.9'],
  play: ['m8 5 11 7-11 7Z'], download: ['M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5'],
  close: ['m6 6 12 12M6 18 18 6'], plus: ['M12 5v14M5 12h14'],
  check: ['m5 12 4 4L19 6'], chevron: ['m9 5 7 7-7 7'],
  calendar: ['M8 2v4M16 2v4M3 10h18M5 4h14a2 2 0 0 1 2 2v14H3V6a2 2 0 0 1 2-2', 'm8 15 3 3 5-5'],
  invite: ['M3 5h18v14H3ZM3 5l9 8 9-8'],
  density: ['M8 6h13M8 12h13M8 18h13M3 6h1M3 12h1M3 18h1'],
  save: ['M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h12l4 4v12a2 2 0 0 1-2 2ZM7 3v6h10V3M7 21v-7h10v7'],
  trash: ['M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7'],
  arrow: ['M19 12H5m7-7-7 7 7 7'], info: ['M12 8h.01M12 11v6', 'M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0'],
}
const aliases = {'mdi-refresh':'refresh','mdi-tune-variant':'settings','mdi-play':'play','mdi-download':'download','mdi-close':'close','mdi-plus':'plus','mdi-content-save-outline':'save','mdi-delete-outline':'trash','mdi-format-line-spacing':'density','mdi-calendar-check-outline':'calendar','mdi-account-group-outline':'invite','mdi-email-plus-outline':'invite'}
const data = computed(() => paths[aliases[props.name] || props.name] || paths.info)
</script>
<template><svg :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path v-for="(d, index) in data" :key="index" :d="d" /></svg></template>
