# 开发与发布

仓库只维护签到与邀请两个插件。功能源码在 `plugins.v3/ptsigninkeeper` 和 `plugins.v3/ptinvitekeeper`，`plugins.v2/` 由 `scripts/build_v2.py` 生成，避免站点规则分叉。V2 的宿主桥接代码在 `scripts/compat_v2/`。共享后端测试在 `tests/v3/`，V2 边界测试在 `tests/v2/`，前端源码、测试和构建脚本在各 V3 源目录的 `frontend/`。

## 验证

使用 Node.js 22、Python 3 和 Docker：

```sh
python3 scripts/build_v2.py
bash scripts/test.sh
python3 scripts/check_release.py
python3 scripts/package_release.py
```

后端测试在隔离容器内运行，禁用网络，配置目录使用临时路径，不读取生产配置。默认使用经验证的 MoviePilot 镜像摘要，可通过 `MOVIEPILOT_TEST_IMAGE` 指定其他兼容镜像。

前端修改后先运行对应 `frontend/` 的 `npm run build`，再运行 `python3 scripts/build_v2.py` 同步。测试及 CI 只检查生成结果，不自动隐藏未同步的改动。V2 与 V3 使用独立镜像和临时配置，V2 镜像可用 `MOVIEPILOT_V2_TEST_IMAGE` 覆盖。

提交 `dist/assets/` 构建产物和 V2 生成目录。`assets/` 保存随插件安装的 WebP 图标；仓库 `icons/` 保存市场使用的 PNG 和 SVG 原稿。

## 发布

两个插件使用相同版本。同步更新插件类的 `plugin_version`、`package.v3.json`、前端 package 与 lock 文件和说明文档，再生成 V2 目录和 `package.v2.json`，通过检查后创建 `vX.Y.Z` 标签。发布工作流生成一个 Release，包含四个 ZIP（两个插件分别提供 V2/V3 包）和 `SHA256SUMS`。

组件构建变化必须使用新的发布版本，不能覆盖已经发布的同版本入口，避免浏览器长期缓存旧 `remoteEntry.js` 后引用已移除的组件文件。

市场索引使用源码安装模式，避免单个统一 Release 与宿主按插件 ID 查找标签的机制冲突。安装源码时也会获取已构建前端和图标。

首版工作流支持明确指定 `consolidate_initial_history=true`：仅当版本为 v1.0.0 且提交只有一个无父提交时，删除本仓旧 PT 管家 Release。此开关仅用于仓库首次整理，默认关闭。

提交使用 `<type>(<scope>): <中文摘要>`，只包含同一功能范围的修改。不要提交账号信息、Cookie、API Key、个人验收记录或生产配置。
