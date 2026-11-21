# Browserless

[返回主页](../README.md)

Browserless 用于处理图片验证码、Cloudflare、Turnstile 等需要真实浏览器的站点。普通 HTTP 签到不会消耗 Browserless 额度。

## 快速配置

1. 打开 <https://browserless.io/signup/email?plan=free> 注册账号。
2. 打开 <https://browserless.io/account/> 获取 Token。
3. 在插件设置中填写 Token，并勾选“Browserless 站点白名单”。
4. Browserless 地址保持默认：

```text
https://production-sfo.browserless.io
```

也可以使用：

- `https://production-ams.browserless.io`
- `https://production-lon.browserless.io`

## 私有部署

支持填写根地址：

```text
http://192.168.1.10:3000
```

插件会自动补全 `/stealth/bql`。也可以直接填写完整端点，例如：

```text
http://192.168.1.10:3000/chromium/bql
```

自建实例关闭鉴权时，Token 可以留空。

## 多 Token 轮换

插件支持自由增加、删除任意数量的 Browserless Token，空项和重复项会自动忽略：

- 当前 Token 返回额度不足或鉴权失败时，自动尝试下一个。
- 成功使用的 Token 会被记住，下一轮优先复用。
- 仅实际调用 Browserless 时消耗服务额度，额度以服务商账户为准。

## 站点白名单

- 只有白名单中的站点才允许调用 Browserless，覆盖直接调用、HTTP 防护回退、API Key 防护回退及本机浏览器回退。
- 未配置或清空白名单时全部禁止；保存、重载后仍保持为空。
- 支持搜索、勾选、取消及清空。选择搜索结果只保存这些站点的 ID，后续新增站点不会自动获得权限。
- 白名单不会强制站点使用 Browserless，也不会将站点自动加入签到范围；HTTP、专用接口及本机 Playwright 仍按原规则运行。
- 自定义规则即使指定 `engine: browserless`，也必须满足白名单。被拦截时结果会说明“站点不在 Browserless 白名单中，已禁止调用”。

## 注意事项

- Browserless 回退会把站点 Cookie 发送到配置的 Browserless 服务，请确认服务地址可信。
- 能否通过 Cloudflare / Turnstile 仍取决于站点风控和 Browserless 节点。
- 对出口 IP 敏感的人机验证站点，云端 Browserless 可能触发异地登录，建议自建 Browserless 或使用本机 Playwright。
- 普通站点优先使用本机 Playwright 或 HTTP，不会主动消耗远程额度。

---

[返回主页](../README.md)
