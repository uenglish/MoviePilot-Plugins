# PT 管家插件

serendipity 的 MoviePilot V2 / V3 插件仓库，专注 PT 站点签到与邀请管理。

| 插件 | 功能 |
| --- | --- |
| <img src="icons/pt-signin-keeper-v1.png" width="40" alt="签到日历"> **PT 签到管家** | 定时签到、登录核验、奖励记录、AI 辅助答题、失败重试与历史查询 |
| <img src="icons/pt-invite-keeper-v1.png" width="40" alt="邀请函"> **PT 邀请管家** | 邀请资格、可用名额、成员状态、异常重试与历史查询 |

两个插件沿用 MoviePilot 原生主题，菜单位于“整理”分组，设置从各自页面内打开。支持搜索、筛选、分页、折叠、密度切换和 CSV 导出。页面适配浅色／深色主题与手机宽度；设置分组定位，输入组件统一聚焦反馈，站点批量选择覆盖所有搜索结果。

## 安装

支持 MoviePilot V2 **2.15.6** 和 V3 **3.2.6 或以上**。更早的 V2 未验证，不声明兼容。在插件市场添加同一仓库，宿主会选择对应版本目录：

```text
https://github.com/uenglish/MoviePilot-Plugins
```

市场中选择“PT 签到管家”和“PT 邀请管家”。首次启用前设置站点范围和执行计划。站点 Cookie / API Key 由 MoviePilot 站点管理提供，插件不附带账号信息。

两个插件独立维护版本号和发行记录，更新其中一个不会要求另一个升级。每个插件的 Release 提供该插件的 V2、V3 安装包和 `SHA256SUMS`；标签分别使用 `PTSignInKeeper_vX.Y.Z`、`PTInviteKeeper_vX.Y.Z`。文件名含 `mpv2` 的 ZIP 用于 V2，其余用于 V3。市场索引使用源码安装模式（`release: false`），宿主按各插件版本检查更新，源码目录包含预构建前端和图标。历史统一发行版继续保留。

V2 保留原生主题页面、“整理”分组入口、内嵌设置、分页与筛选。签到和邀请通知转换为 V2 支持的分段文本，AI 答题复用 V2 智能助手配置。未配置 AI 时继续使用站点原有方式。

## 使用说明

- [PT 签到管家](plugins.v3/ptsigninkeeper/README.md)
- [PT 邀请管家](plugins.v3/ptinvitekeeper/README.md)
- [开发与发布](docs/DEVELOPMENT.md)
- [版本说明](docs/RELEASE.md)
- [V2 兼容说明](docs/COMPATIBILITY.md)

登录有效只表示会话可用，保活请结合站点最近访问记录和站点规则判断。奖励缺失显示“未获取”，不会用余额推算。AI 辅助可能答错，未配置 AI 时沿用站点适配的默认方式。

## 许可

按 [GNU GPL v3](LICENSE) 分发。维护者与发布者为 **serendipity**。保留代码的来源与许可说明见各插件 `NOTICE.md`。
