# 开发与发布

仓库只维护 `plugins.v3/ptsigninkeeper` 和 `plugins.v3/ptinvitekeeper`。对应后端测试在 `tests/v3/`，前端源码、测试和构建脚本在各插件的 `frontend/`。

## 验证

使用 Node.js 22、Python 3 和 Docker：

```sh
bash scripts/test.sh
python3 scripts/check_release.py
python3 scripts/package_release.py
```

后端测试在隔离容器内运行，禁用网络，配置目录使用临时路径，不读取生产配置。默认使用经验证的 MoviePilot 镜像摘要，可通过 `MOVIEPILOT_TEST_IMAGE` 指定其他兼容镜像。

前端修改后提交 `dist/assets/` 构建产物。`assets/` 保存随插件安装的 WebP 图标；仓库 `icons/` 保存市场使用的 PNG 和 SVG 原稿。

## 发布

两个插件使用相同版本。同步更新插件类的 `plugin_version`、`package.v3.json`、前端 package 与 lock 文件和说明文档，通过检查后创建 `vX.Y.Z` 标签。发布工作流会生成一个 Release，包含两个 ZIP 和 `SHA256SUMS`。

市场索引使用源码安装模式，避免单个统一 Release 与宿主按插件 ID 查找标签的机制冲突。安装源码时也会获取已构建前端和图标。

首版工作流支持明确指定 `consolidate_initial_history=true`：仅当版本为 v1.0.0 且提交只有一个无父提交时，删除本仓旧 PT 管家 Release。此开关仅用于仓库首次整理，默认关闭。

提交使用 `<type>(<scope>): <中文摘要>`，只包含同一功能范围的修改。不要提交账号信息、Cookie、API Key、个人验收记录或生产配置。
