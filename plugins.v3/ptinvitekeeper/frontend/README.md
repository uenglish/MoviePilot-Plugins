# 插件界面构建

使用 Node.js 20+，在此目录执行 `npm ci`、`npm test`、`npm run check`、`npm run build`。构建产物写入插件的 `dist/`。

Vue 由 MoviePilot 宿主共享，界面使用局部 CSS，不引入 Vuetify 或全局图标字体。发布只包含源码、锁文件与 dist，不包含 node_modules。
