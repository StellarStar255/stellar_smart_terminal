# 排障记录：更新下载报 SSL: UNEXPECTED_EOF_WHILE_READING

- 日期：2026-07-18
- 环境：Ubuntu（Linux 6.14），Clash Verge Rev（verge-mihomo 核心，混合端口 7897）
- 症状：Smart Terminal「Software Update」下载更新失败，弹窗报
  `urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol`；
  同时系统整体下载慢（浏览器/apt 等都慢），即使 Clash 里已开 TUN 模式。

## 结论（TL;DR）

TUN 开关是开的，但核心**启动 TUN 失败**，实际没有任何流量走代理；
GUI 程序（含本应用的更新器）从桌面启动继承不到终端的 `http_proxy`，
于是裸连 GitHub，下载 release 大文件时 TLS 连接被中途掐断。

两层修复：

1. 系统层：给 mihomo 核心补上创建 TUN 设备的权限（根因）。
2. 应用层：`app_updater.py` 加本机代理端口探测 fallback（兜底，
   commit `78f4397`）。

## 诊断过程

按顺序查，每步的判据：

1. **代理节点本身是否够快** —— 排除机场问题：

   ```bash
   curl -sx http://127.0.0.1:7897 -o /dev/null \
     -w '%{speed_download} B/s\n' 'https://speed.cloudflare.com/__down?bytes=20000000'
   ```

   实测 16.6 MB/s，节点没问题。

2. **TUN 网卡是否真的存在** —— `ip addr` 里应有一块 TUN 网卡
   （mihomo 的叫 `Meta`，按名字 grep "tun" 会漏掉）。当时没有，
   默认路由直接走物理网卡 → TUN 实际未生效。

3. **看核心日志找失败原因**（Clash Verge Rev 的 sidecar 日志）：

   ```
   ~/.local/share/io.github.clash-verge-rev.clash-verge-rev/logs/sidecar/
   ```

   命中关键行：

   ```
   level=error msg="Start TUN listening error: configure tun interface: operation not permitted"
   ```

   原因：Verge 启动时没连上 root 服务（`Service IPC Path: No such
   file or directory`，启动顺序竞争），降级为普通用户身份的 sidecar
   模式运行核心，没权限建 TUN 设备。

4. **确认更新器的处境** —— 模拟无代理环境变量的 GUI 直连：

   ```bash
   curl -s --noproxy '*' -o /dev/null -w '%{speed_download} B/s\n' \
     --max-time 20 https://github.com/StellarStar255/stellar_smart_terminal/releases/latest
   ```

   只有 23 KB/s；小请求（API 查版本）能过，大文件下载中途被断，
   正对应弹窗里的 `UNEXPECTED_EOF_WHILE_READING`。

## 修复

### 系统层（根因）

给核心二进制加网络管理能力，之后在 Verge 里把 TUN 关掉再打开：

```bash
sudo setcap cap_net_admin,cap_net_bind_service=+ep /usr/bin/verge-mihomo
```

顺手把 TUN 栈从 `gvisor` 改成 `mixed`（gvisor 是用户态协议栈，明显偏慢）。

验证生效：

- `ip addr` 出现 `Meta` 网卡（198.18.0.1，fake-ip 网段）；
- 无代理环境变量直连测速恢复正常（修复后 8.5 MB/s，此前 23 KB/s）；
- `curl -s --noproxy '*' https://api.ip.sb/geoip` 出口 IP 变为代理节点。

⚠️ **setcap 绑定在文件上，Clash Verge Rev 升级替换二进制后会丢失**。
若日后 TUN 又失效、日志再报 `operation not permitted`，重跑上面那条
setcap 即可。

### 应用层（兜底，commit `78f4397`）

`app_updater.py` 的 `_urlopen` 增加 fallback：当
`urllib.request.getproxies()` 为空（环境变量和系统设置都没配代理）时，
TCP 探测本机常见代理端口 7897（Clash Verge Rev）/ 7890（Clash 经典），
谁在监听就用谁作 HTTP 代理；都不通则维持直连，对无代理用户零影响。

这样即使 TUN 再次失效、系统代理也没配，只要本机代理还开着，
更新器就能自己找到端口正常下载。单测见
`tests/test_app_updater.py::TestLocalProxyFallback`。
