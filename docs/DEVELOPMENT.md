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

两个插件独立维护版本。只更新本次有改动的插件类 `plugin_version`、`package.v3.json` 对应条目、该插件前端 package 与 lock 文件和说明文档，再生成 V2 目录和 `package.v2.json`。同一个插件的 V2/V3 版本须一致，两个插件之间无需一致。

发行说明保存为 `docs/releases/<插件ID>_v<版本>.md`，并更新 `docs/RELEASE.md` 索引。检查通过后创建对应标签，例如 `PTSignInKeeper_v1.4.1` 或 `PTInviteKeeper_v1.4.1`。标签推送触发工作流，只打包和发布该插件的两个平台包及 `SHA256SUMS`。

```sh
python3 scripts/package_release.py --tag PTSignInKeeper_v1.4.1
git tag -a PTSignInKeeper_v1.4.1 -m 'PT 签到管家 1.4.1'
python3 scripts/publish_release.py --tag PTSignInKeeper_v1.4.1 --dry-run
git push origin main PTSignInKeeper_v1.4.1
```

指定标签的产物位于 `output/<标签>/`，不同插件的校验清单互不覆盖；不指定 `--tag` 时生成全部插件的验证包。发布前检查标签与当前提交一致、目标插件版本一致以及附件哈希；已公开版本仅允许校验，不覆盖。测试工作流仍验证两款插件，防止共享构建流程造成回归。

组件构建变化必须使用新的发布版本，不能覆盖已经发布的同版本入口，避免浏览器长期缓存旧 `remoteEntry.js` 后引用已移除的组件文件。

市场索引继续使用源码安装模式，安装时获取对应平台目录、已构建前端和图标。版本解耦不改变现有配置和数据命名空间。

历史统一标签与发行附件保留，不再提供删除历史发行记录的首版整理选项。

提交使用 `<type>(<scope>): <中文摘要>`，只包含同一功能范围的修改。不要提交账号信息、Cookie、API Key、个人验收记录或生产配置。
