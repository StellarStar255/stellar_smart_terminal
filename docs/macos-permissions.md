# macOS 权限与「责任进程」机制 / macOS permissions & the "responsible process"

在 Stellar 终端里启动的程序（子进程）想用麦克风、语音识别、摄像头、日历等
**受保护资源**时，权限归属不看子进程本身，而看**发起它的应用**——这套机制
叫「responsible process（责任进程）」。理解它能解释一类常见现象：

> 「同一个 Python 程序，直接在 Terminal.app / 从源码跑没问题，一放进
> Stellar（或某个打包终端）里跑就闪退 / 拿不到摄像头。」

When a program you launch inside Stellar Terminal (a child process) uses a
**protected resource** (microphone, speech recognition, camera, calendar, …),
macOS attributes the permission not to the child but to the **app that launched
it** — the *responsible process*. This explains a common symptom: *the same
Python program works from Terminal.app / from source, but crashes or can't reach
the camera when run inside Stellar (or any bundled terminal).*

---

## 机制 / How it works

macOS 的隐私系统（TCC）在子进程触碰受保护资源时，往上追到**责任进程**
（通常是启动整条进程树的那个 `.app`），然后：

1. 读责任进程 `Info.plist` 里对应的 `NS*UsageDescription` 用途声明；
2. **缺声明** → 依资源类别，子进程调用的**那一刻被系统直接终止（闪退）**，
   连授权弹窗都没有（麦克风/语音识别等属此类）；
3. **有声明** → 弹一个系统授权窗，**归属显示责任进程**（比如
   "Stellar Smart Terminal 想访问摄像头"），用户点允许后即可使用。

因此：

- **源码运行 / `run_gui.command`**（走 `open /opt/anaconda3/python.app`）→
  责任进程是 `python.app`，它自带用途声明，故一直正常。
- **打包版 Stellar.app** → 责任进程是 `Stellar Smart Terminal.app`，必须由
  **它的 `Info.plist`** 声明这些用途，里面跑的程序才能申请权限。

The child inherits the responsible process for TCC. A bare `python` process has
no `Info.plist`/usage strings of its own — macOS checks the responsible `.app`'s.
That's why the fix must live in **Stellar's `Info.plist`**, not the child.

---

## Stellar 的处理 / What Stellar declares

照 Terminal.app / iTerm2 的做法，Stellar 在 `smart_terminal.spec` 的
`info_plist` 里**预声明了一整套用途描述（29 项，v1.14.31 起）**，覆盖
麦克风、语音识别、摄像头、日历（含 14+ 完整/只写拆分）、通讯录、提醒、照片、
定位各变体、蓝牙、本地网络、可移动卷/网络卷、Siri、Focus、Nearby Interaction、
Motion、系统管理、AppleEvents、文件夹访问等。

**改动位置**：`smart_terminal.spec` → `BUNDLE(... info_plist={...})`。新增受
保护资源时在此加对应 `NS*UsageDescription` 键（值是给用户看的说明文案），
**重新打包发版**后生效——`Info.plist` 是构建期写入的，改了必须出新版并让用户
更新+重启才生效，光改代码运行的旧 app 无效。

Declared in `smart_terminal.spec` under `BUNDLE(... info_plist=...)`. Add new
`NS*UsageDescription` keys there and **rebuild/release** — the plist is baked at
build time, so users must update and relaunch for it to take effect.

---

## Info.plist 声明不了的四类 / What a plist CANNOT pre-declare

这四类权限 macOS **不走 `Info.plist` 声明键**，无法预打包进去——必须**用户手动**
在「系统设置 → 隐私与安全性」里，把 **Stellar Smart Terminal** 加进对应列表
（归属同样是责任进程）：

- 屏幕录制 / Screen Recording
- 辅助功能 / Accessibility
- 完全磁盘访问 / Full Disk Access
- 输入监控 / Input Monitoring

若终端里的程序需要这四类之一，升级 app 也不会自动弹窗；请手动授权。

These four have **no** `Info.plist` key. The user must add Stellar to the
matching list under **System Settings → Privacy & Security** manually.

---

## 排查 / Troubleshooting

**现象：更新到已声明的版本后仍闪退 / 授权窗不弹。**
多为 TCC 缓存了旧状态（未签名 app 尤其常见——见下）。重置对应类别后重开：

```bash
tccutil reset SpeechRecognition com.stellar.smart-terminal
tccutil reset Microphone       com.stellar.smart-terminal
tccutil reset Camera           com.stellar.smart-terminal
```

**现象：OpenCV / 摄像头「not authorized… requesting… failed to initialize」。**
这是 macOS 上 OpenCV 的经典行为：**首次调用触发授权请求但不等异步结果就立刻
返回失败**。授权后**把程序再跑一遍**，第二次即可初始化。

**现象：OrbbecSDK「No device found」。**
这是在找 Orbbec 深度相机**硬件**，没插就找不到，与权限无关，可忽略。

---

## 签名的影响（已知限制）/ Signing caveat

Stellar 目前**未做代码签名 + 公证**。未签名 app 的 TCC 身份是基于路径/内容的
临时标识，**每次更新（内容变了）身份可能变**，导致**已授过的权限不跨版本持久**，
需重新授权一次。`Info.plist` 声明能根治「闪退」，但要让授权稳定持久，最终仍需
代码签名+公证（见 backlog）。

Unsigned apps get an ad-hoc TCC identity that can change across updates, so
granted permissions may not persist. Declaring usage strings fixes the *crash*;
persistent grants need proper code signing + notarization.
