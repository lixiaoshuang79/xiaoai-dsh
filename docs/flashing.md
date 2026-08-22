# 小爱音箱刷机指南（flashing.md）

> 目标：把小米 AI 音箱刷入可 root 的系统，改造成本地大模型语音管家。
> 刷机完成后：唤醒词保持原生「小爱同学」，回答由本地大模型完成（部署完整流程见 `deploy.md`）。

## 风险提示

- 刷机有变砖风险，操作前务必确认音箱机型与固件镜像匹配；
- 刷机可能失去官方保修；本项目与上游项目均按 MIT 协议提供，刷机后果自负；
- 刷机全程保持 USB 连接稳定、不要中途断电。

---

## 0. 前置条件

### 0.1 硬件

| 需要 | 说明 |
|---|---|
| 一台 macOS 电脑 | 刷机工具仅支持 macOS（`flash` 脚本会检查 `uname`） |
| 一根 USB 数据线 | 必须能传数据（不是纯充电线） |
| 目标音箱 | 本方案以 OH2P（小爱音箱 Pro）系列验证；其他机型请按上游说明核对兼容性 |

### 0.2 上游 open-xiaoai 下载说明

本项目的刷机流程建立在 [idootop/open-xiaoai](https://github.com/idootop/open-xiaoai) 之上（MIT 协议，作者 Del Wang，项目已停止维护，但 Release 资源仍可下载）。需要从上游获取三样东西：

| 资源 | 用途 | 放哪里 |
|---|---|---|
| `update`（macOS 刷机工具，二进制） | 通过 USB 与音箱 bootloader 通信 | `probe/macos/update`（与 `probe/flash` 的查找路径一致） |
| 固件镜像（如 `root.squashfs`） | 刷入 system 分区，提供 root SSH | 任意位置，记住路径 |
| `client`（客户端二进制，约 900KB） | 刷机后部署到音箱 `/data/open-xiaoai/client`，负责与 Mac 上 migpt 的 4399 WebSocket 通信 | 刷机后随其他文件一起 scp 到音箱 |

下载方式：打开上游仓库 → **Releases** 页面，按机型选择资源；或 `git clone` 仓库后在对应目录查找（macOS 工具与固件通常按机型组织）。**务必按你的音箱机型选择镜像，不要混刷。**

> 上游项目提供「探针利用」——通过 USB 刷机口进入 bootloader 的利用链路，`update` 工具正是走这条链路与音箱通信；本仓库 `probe/flash` 是对其命令行风格的封装，`probe/` 下的 `usbprobe*.c` 则是我们自己写的 USB 诊断程序。

### 0.3 本仓库 probe/ 目录

| 文件 | 说明 |
|---|---|
| `flash` | macOS 刷机封装脚本（`connect` / `delay` / `switch` / `system` 四个子命令），内部调用 `update` 工具 |
| `usbprobe.c` / `usbif.c` / `usbprobe3.c` | 自研 USB 诊断探针（libusb 枚举 USB 设备），用于「设备连不上」时排查（编译方法见 §5.1） |

---

## 1. 刷机步骤

### 步骤 1：接线并进入刷机模式

1. 用 USB 数据线连接音箱与 Mac；
2. **拔掉音箱电源，再重新插上**（保持 USB 连接）——这一步让音箱的 bootloader 进入 USB 刷机模式并等待主机连接。

### 步骤 2：连接设备

```bash
cd <仓库路径>/probe
./flash connect
```

脚本会循环执行 `update identify 7` 直到识别到设备，输出「✅ 设备已连接」。

- 长时间连不上：拔插电源重试；换 USB 口 / 换线；用 USB 探针排查（见 §5.1）。

### 步骤 3：设置启动延时（可选）

```bash
./flash delay 15
```

设置 bootdelay（秒），给启动过程留出中断窗口；不需要可跳过。

### 步骤 4：切换启动分区

```bash
./flash switch boot0
```

内部执行 `update bulkcmd "setenv boot_part boot0"` + `saveenv`，把启动分区切到 boot0。

### 步骤 5：刷入 root 固件

```bash
./flash system system0 <固件镜像路径>
# 例如：./flash system system0 root.squashfs
```

内部执行 `update partition system0 <文件>`，把固件写入 system0 分区。输出「✅ 刷入成功」即完成；报 ERR 时见 §5.2。

### 步骤 6：重启并配网

拔插电源正常开机。音箱与官方系统一样需要联网（米家 App 配网或按上游说明），并确保与 Mac 处于**同一局域网**（建议在路由器里给音箱静态绑定 IP）。

---

## 2. 验证 root SSH

音箱联网后，找到音箱 IP（路由器后台或米家 App 设备信息），然后：

```bash
ssh root@<音箱IP>
# 密码：open-xiaoai（上游公开默认值！）
```

登录后验证：

```bash
uname -a                                  # 有系统信息输出
ls -l /etc/rc.local                       # 开机钩子文件存在
cat /etc/rc.local                         # 应包含执行 /data/init.sh 的行（上游刷机流程自带；没有则手动加）
df -h                                     # 能看到 system0 分区
```

---

## 3. 部署音箱端文件

### 3.1 文件清单

以下文件部署到音箱 `/data/open-xiaoai/`：

| 文件 | 来源 | 说明 |
|---|---|---|
| `client` | 上游 releases（约 900KB） | 连接 Mac 上 migpt 的 4399 WebSocket |
| `server.txt` | 自建 | 一行写入 Mac 局域网 IP（不带端口） |
| `native-block.sh` | 本仓库 `speaker/` | 官方执行拦截 + 直连模式兜底 |
| `direct-mode.sh` | 本仓库 `speaker/` | 每 5 秒探测 Mac 存活，连续失败 / 成功时切换模式 |
| `restart-aivs.sh` | 本仓库 `speaker/` | 带 LD_PRELOAD hook 的干净重启 |
| `hook_final.so` | 本仓库 `speaker/hook/` | 预编译 ARM LD_PRELOAD 拦截器（6504 字节） |
| `config.env` | 后台生成（见 deploy.md）或手写模板 | 大模型直连配置（降级模式用） |
| `system_prompt.txt` | 后台生成或手写 | 降级模式系统提示词 |
| `silent.wav` | 自建（静音 wav） | 静音官方提示音（保留 `wakeup_*` 唤醒音） |

另：`speaker/init.sh` 部署到 `/data/init.sh`（由 `/etc/rc.local` 开机调起）。`init.sh` 的作用：静音官方提示音、固定 TTS 男声音色（XiaoMi_M88）、启动 `native-block.sh` 与 `direct-mode.sh`、用 `restart-aivs.sh` 注入 hook、读取 `server.txt` 启动 `client`。

### 3.2 准备 config.env 与 system_prompt.txt

如果还没跑 deploy.md 的配置后台，可手写（内容与后台生成一致）：

```bash
# /data/open-xiaoai/config.env（音箱端大模型直连配置）
LLM_BASE="<大模型API地址>"
LLM_KEY="<API Key>"
LLM_MODEL="<快速模型名>"
MAC_IP="<Mac局域网IP>"
```

`system_prompt.txt` 示例（降级模式人设；后台生成版会自动追加「后台大脑离线」的降级说明）：

```
你是小爱，这家的智能语音管家，称呼用户为先生。回答口语化、简洁，一般不超过三句话，中文。
```

### 3.3 生成 silent.wav（Mac 上执行）

```bash
python3 - <<'PY'
import wave
with wave.open("silent.wav", "wb") as w:
    w.setnchannels(1)          # 单声道
    w.setsampwidth(2)          # 16bit
    w.setframerate(16000)      # 16kHz
    w.writeframes(b"\x00\x00" * 16000)   # 1 秒静音
PY
```

### 3.4 上传（scp）

> 音箱端没有 `base64` / `od` 命令，文件一律用 scp 传输，**不要**用「echo base64 文本 | base64 -d」之类的方式部署。

scp 会交互询问密码（默认 `open-xiaoai`）；脚本化部署可用 `expect` 包装（见下文示例）。

```bash
cd <仓库路径>

# 核心脚本与 hook
scp speaker/native-block.sh speaker/direct-mode.sh speaker/restart-aivs.sh \
    root@<音箱IP>:/data/open-xiaoai/
scp speaker/hook/hook_final.so root@<音箱IP>:/data/open-xiaoai/

# client（来自上游 releases）与静音文件
scp client silent.wav root@<音箱IP>:/data/open-xiaoai/

# 配置与提示词（后台生成或手写）
scp config/generated/speaker/config.env root@<音箱IP>:/data/open-xiaoai/ 2>/dev/null || true
scp config/generated/speaker/system_prompt.txt root@<音箱IP>:/data/open-xiaoai/ 2>/dev/null || true

# server.txt：一行 Mac 局域网 IP
printf '%s\n' "<Mac局域网IP>" > server.txt
scp server.txt root@<音箱IP>:/data/open-xiaoai/

# init.sh 部署为 /data/init.sh
scp speaker/init.sh root@<音箱IP>:/data/init.sh
```

`expect` 非交互示例（macOS 自带 expect）：

```bash
expect -c '
spawn scp config.env root@<音箱IP>:/data/open-xiaoai/
expect "password:"
send "<音箱root密码>\r"
expect eof'
```

### 3.5 设置权限与安装 init 钩子（SSH 到音箱执行）

```bash
ssh root@<音箱IP>

chmod +x /data/open-xiaoai/client /data/open-xiaoai/*.sh /data/init.sh

# 确认 /etc/rc.local 已执行 /data/init.sh
cat /etc/rc.local
```

如果 `/etc/rc.local` 里没有执行 `/data/init.sh` 的行（上游刷机流程通常自带该钩子），手动追加：

```sh
# /etc/rc.local 追加一行：
/data/init.sh &
```

> **坑**：音箱后台进程必须用 `( cmd & )` 双括号包住并后台化，否则 SSH / exec 会话一退出进程就死。`init.sh` 内部已按此写法，手动加钩子时也要遵守。

### 3.6 立即生效（或重启）

```bash
# 方式一：手动跑一次（会启动 native-block / direct-mode / client）
sh /data/init.sh

# 方式二：重启音箱
reboot
```

验证：

```bash
pgrep -f client                          # client 在跑
tail -n 20 /data/open-xiaoai/client.log  # 无报错
pgrep -f native-block                    # 拦截脚本在跑
pgrep -f direct-mode                     # 存活探测在跑
```

Mac 侧 migpt 日志应能看到音箱接入（完整栈部署见 `deploy.md`）。

---

## 4. 修改默认密码

默认 root 密码 `open-xiaoai` 是**上游公开默认值**——任何能碰到你音箱的人都知道它，刷机后第一件事就是改掉：

```bash
ssh root@<音箱IP>
passwd        # 按提示输入新密码两次
```

进阶（可选）：改用 SSH 密钥登录。把 Mac 的公钥追加到音箱 `/root/.ssh/authorized_keys`，之后可考虑禁用密码登录；是否禁用视音箱 sshd 而定——**至少先改密码**。

---

## 5. 常见问题

### 5.1 `./flash connect` 一直等不到设备

- 先确认 USB 线是数据线（不是纯充电线）；
- **拔掉音箱电源重新插上**——每次重新上电都会进入刷机模式等待；
- 换一个 USB 口试试；
- 用自研探针排查 USB 枚举：

```bash
brew install libusb pkg-config
cd <仓库路径>/probe
cc -o usbprobe usbprobe.c $(pkg-config --cflags --libs libusb-1.0)
./usbprobe
```

能看到音箱的 VID:PID 说明 USB 链路正常。`usbprobe3` 会把日志写到 `/tmp/probe3.log`（分步记录 libusb 初始化与设备枚举，适合判断卡在哪一步）；`usbif.c` 打印接口明细（`cc -o usbif usbif.c $(pkg-config --cflags --libs libusb-1.0)`），可用于对比插拔前后的枚举差异。

### 5.2 `update` 输出 ERR / 刷入失败

- 确认固件文件存在、路径正确；
- 确认已先执行 `./flash connect` 成功、`./flash switch boot0` 已执行；
- 分区名写对（如 `system0`）；
- 换 USB 口，重新插电后再试一次。

### 5.3 SSH 连不上

- 确认音箱已联网、IP 正确（路由器后台 / 米家 App）；
- 确认系统正常启动（能联网说明固件已生效）；
- 默认密码 `open-xiaoai` 区分大小写；若之前改过，用新密码；
- 确认局域网可达：`ping <音箱IP>`。

### 5.4 `client` 没起来

- 看 `/data/open-xiaoai/client.log`；
- 确认 `server.txt` 是 Mac 局域网 IP 一行、无多余字符（heredoc 部署会多一个尾随空行，必要时用 `printf` 重写）；
- 确认 Mac 上 migpt 已启动（4399 端口在听，见 deploy.md）。

### 5.5 部署脚本「一执行就死」

音箱的 SSH / exec 会话结束会清理后台子进程，必须用 `( cmd & )` 双括号包裹再后台化：

```sh
( /data/open-xiaoai/native-block.sh >/dev/null 2>&1 & )
```

不要直接 `sh /data/open-xiaoai/native-block.sh &`。

### 5.6 heredoc 写文件末尾多一个空行

用 heredoc 在音箱上生成脚本 / 文本会**多一个尾随空行**。需要 MD5 校验时，先在本地补一个换行再比对；日常部署优先用 scp 传文件，避免该问题。

---

## 附：刷机后下一步

刷机完成 + 音箱端文件就位后，回到 Mac 侧按 `deploy.md` 完成：配置后台 → 启动桥 / hass-mcp / migpt → launchd 常驻 → 验证清单。
