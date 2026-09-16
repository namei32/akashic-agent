# Android 手机连接（Cloudflare）

当前手机入口为 **`wss://mobile.roxyagent.org/ws`**，由现有 `roxy-mobile` Cloudflare Tunnel
转发到这台 Mac 的 `https://127.0.0.1:6323`。Android 手机无需安装或连接 Tailscale。

## 手机端操作

1. 安装 [Akashic Mobile v0.8.43](https://github.com/kachofugetsu09/akashic-mobile/releases/tag/v0.8.43)
   发布页中的 `Akashic-Mobile-v0.8.43.apk`。
2. 在 Mac 打开 <http://127.0.0.1:2238/chat>，点击左侧“连接手机”。
3. 在手机 Akashic Mobile 中选择“扫描电脑”，扫描电脑显示的新二维码。
4. 核对手机与电脑上的六位确认码。相同后，在电脑点击“确认并连接”。
5. 等待“手机已连接”，然后在手机发送一条消息，确认收到回复。

应使用切换到 Cloudflare 后新生成的二维码。二维码约八分钟后失效；过期时关闭配对窗口，
重新点击“连接手机”。二维码包含一次性配对材料，不要提交到 Git 或公开分享。

`mobile.roxyagent.org` 提供手机协议入口，根路径没有普通网页，返回 404 是预期行为。
电脑上的 `127.0.0.1:2238` 用于配对管理，不能在手机上作为电脑地址使用。

## 连接与凭据

- 桌面网页监听 `127.0.0.1:2238`，移动网关监听 `127.0.0.1:6323`。
- Cloudflare 使用系统可信证书服务公网 WSS；回源验证本机专用证书，未跳过证书校验。
- 回源 CA Pool 使用本机 `cloudflare/origin-cert.pem`；Origin Server Name 与现有证书的 SAN
  保持一致。这个证书标识不要求 Mac 或手机继续连接 Tailscale。
- Android 继续校验二维码绑定的应用身份，并通过一次性配对和设备签名进行认证。
- Tunnel token 位于 `$HOME/.local/share/akashic-learning/cloudflare/tunnel.token`，
  文件权限 `0600`，父目录权限 `0700`。凭据未写入命令行参数、Git 或文档。
- 手机配对数据库与密钥保存在独立学习环境的 `workspace/data/`。
- macOS TLS 私钥通过匿名管道交给 OpenSSL，不生成明文私钥临时文件；Linux 继续使用 memfd。

## 自动启动与启停

Akashic 和 Cloudflare connector 使用两个独立的用户 LaunchAgent，登录后自动启动，
进程退出后由 launchd 重新拉起。关闭浏览器、终端或 Codex 不会停止它们。

查看或管理 Cloudflare connector：

```bash
"$HOME/.local/share/akashic-learning/cloudflare/service.sh" status
"$HOME/.local/share/akashic-learning/cloudflare/service.sh" logs
"$HOME/.local/share/akashic-learning/cloudflare/service.sh" restart
```

同一脚本也支持 `start`、`stop`、`disable`。`stop` 仅暂停本次登录期间的服务，
`disable` 会同时取消以后登录时的自动启动；`start` 重新启用它。

Cloudflare 服务文件为
`$HOME/Library/LaunchAgents/io.namei.akashic-mobile-cloudflare.plist`。
Akashic 本身的管理入口仍为 `$HOME/.local/share/akashic-learning/service.sh`，见
[LOCAL_SETUP.md](LOCAL_SETUP.md)。Mac 仍需开机、联网并保持用户登录；系统休眠设置未改变。

## 验证与排障

电脑端已验证公网 WSS 的证书、`server.challenge` 和本机网关身份；这不等于手机已经配对。
扫码、确认码核对和手机实际收发消息需要在手机端完成。本方案尚未配置 iPhone 浏览器接入。

2026-09-16 已验证连接器的自动恢复：受控退出后约 3.6 秒换用新进程恢复公网 WSS。
本机恢复证据位于 `cloudflare/recovery-check.json`。启停脚本会等待 launchd 完成卸载，
再重新加载服务，避免异步卸载造成的重启竞争。

- Tunnel 为 Down：检查 connector 的服务状态与日志。
- 公网返回 502：检查 Akashic 是否运行，以及本机 6323 网关是否就绪。
- 页面根路径返回 404：该地址提供手机协议，使用 Android 客户端扫码连接。
- 二维码过期：重新生成，不修改已保存的手机数据库或密钥。
- Token 在 Cloudflare 被轮换后，需要更新本机受限文件并重启 connector。

本机 `cloudflare/client-config-before-cloudflare.json` 保留切换前的客户端配置。
