# Android 手机连接

当前电脑端已按 **Android + Tailscale 私有网络** 配置。手机与 Mac 都需要连接到同一个
Tailscale 网络。这个方式不需要公网域名，也不需要开放路由器端口。

## 手机端操作

1. 安装 [Tailscale Android](https://tailscale.com/docs/install/android)，使用与 Mac 相同的账号登录，
   开启连接，并确认设备列表里能看到这台 Mac。
2. 安装 [Akashic Mobile v0.8.43](https://github.com/kachofugetsu09/akashic-mobile/releases/tag/v0.8.43)
   发布页中的 `Akashic-Mobile-v0.8.43.apk`。
3. 在 Mac 打开 <http://127.0.0.1:2238/chat>，点击左侧“连接手机”。
4. 在手机 Akashic Mobile 中选择“扫描电脑”，扫描电脑显示的二维码。
5. 核对手机与电脑上的六位确认码。相同后，在电脑点击“确认并连接”。
6. 等待“手机已连接”，然后在手机发送一条消息，确认收到回复。

二维码约八分钟后失效。过期时关闭配对窗口，再次点击“连接手机”生成新的二维码。
二维码包含一次性配对材料，不要提交到 Git 或公开分享。

## 当前电脑配置

- 桌面网页继续只监听 `127.0.0.1:2238`。
- 手机 WSS 网关仅监听本机 Tailscale IPv4 地址的 `6323` 端口。
- 二维码包含实际地址、应用身份公钥和 TLS 公钥指纹。Android 会校验证书主机名与扫码绑定的指纹。
- 手机通过独立 WSS 网关连接，不是在手机浏览器里访问电脑的 `127.0.0.1`。
- 配对数据库与密钥位于独立学习环境的 `workspace/data/`，未提交到 Git。
- 本机使用项目支持的 `file` master-key provider，文件权限为 `0600`。加密后的 TLS 私钥在
  macOS 上通过匿名管道交给 OpenSSL，不生成明文私钥临时文件；Linux 继续使用 memfd。

当前 Tailscale 地址以本机配置与新生成的二维码为准。本方案还未配置 iPhone 浏览器接入。
手机离开 Wi-Fi 后继续使用移动数据时，需要保持手机 Tailscale 连接。

## 运行与验证

本次已完成九项密钥保护测试、带证书校验的真实 WSS 握手，以及服务重启检查。
这不等于手机已经配对；手机扫码、确认码核对和实际收发消息需要在设备端完成。

Mac 需要保持开机、联网，Tailscale 需要运行。持续运行与启停方法见 [LOCAL_SETUP.md](LOCAL_SETUP.md)。
当前网关绑定 Tailscale 地址；如果 Tailscale 未启动，服务启动可能失败并由 launchd 稍后重试。

需要恢复到启用移动网关前的配置时，原配置备份位于本机数据目录的
`mobile-config-before-enable.json`；通过插件配置接口恢复，保留手机数据库和密钥作为恢复材料。
