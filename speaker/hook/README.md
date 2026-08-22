# speaker/hook — 零竞态拦截器（hook_final.so + hook_tts.so）

音箱端注入 `/usr/bin/mico_aivs_lab` 的 LD_PRELOAD 钩子链（按顺序）：
`hook_final.so:hook_tts.so`。前者拦设备指令词，后者拦官方 TTS 播报，均以
预编译 ARM 二进制为事实来源（词表编译在二进制内，strings 不可见）。

## hook_final.so — 设备指令零竞态拦截

`hook_final.so` 拦截**设备指令词**写入 instruction.log 的 `write()` 调用瞬间击杀
官方进程——云端收不到设备执行请求（零竞态，比脚本轮询日志快一个数量级）。

1. `restart-aivs.sh <hook路径>` 通过 procd 的 `service set` 给 mico_aivs_lab 注入
   `LD_PRELOAD=/data/open-xiaoai/hook_final.so` 环境变量，再干净重启该进程
2. mico_aivs_lab 每次向 `/tmp/mico_aivs_lab/instruction.log` 写
   `RecognizeResult`（含设备指令词：开/关/调/灯/空调/窗帘…）时，hook 先检查内容：
   - 命中设备词 → 立即击杀宿主进程（官方云端下发的执行指令无人接收）
   - 未命中 → 放行原 write
3. procd 不会自动拉起被杀进程，`native-block.sh` 随后用 `restart-aivs.sh`
   干净重启并保持注入（钩子不丢）

## hook_tts.so — 官方 TTS 零竞态拦截（v4）

官方 TTS 唯一播放者是 **mediaplayer**（mibrain 只是 ubus 入口）；本地 AI 的
TTS 与音乐全部走 **miplayer**，杀 mediaplayer 对本地零影响。`hook_tts.so`
在官方写 Speak 指令到 instruction.log 的 write/writev 瞬间**杀光所有
mediaplayer**（必须全杀——重启残留的僵尸进程 comm 仍匹配，只杀首个 pid
可能命中僵尸漏杀真身）。

判别三态：

| 场景 | 判定 | 动作 |
|---|---|---|
| 官方版权失败话术（试听/黑胶/会员/版权/APP） | 特征词 | LEAK-KILL（必杀） |
| 本地 AI 作答中（`/tmp/xdf_our_pending` 15s 内，migpt beginAnswer + speaker.play 写 epoch） | 时间窗 | PEND-KILL |
| 闹钟/音量等官方独占确认（native 放行路径） | 均不命中 | PASS 放行 |

`native-block.sh` 兜底：Speak 落盘后 0.6s 查无存活 mediaplayer（`/proc/stat`
排除 Z 僵尸）才延时重启；官方 TTS 调度失败不重试，杀 mediaplayer 不杀官方
进程 = 无云端补发死循环。直连模式（`/tmp/direct_mode` 存在）钩子放行官方 TTS。

## 部署

```sh
# 音箱上（经 SSH 或 migpt /exec 端点）
cp hook_final.so hook_tts.so /data/open-xiaoai/
/data/open-xiaoai/restart-aivs.sh /data/open-xiaoai/hook_final.so
# 验证注入：主进程 environ 里应有 LD_PRELOAD（链顺序 hook_final.so:hook_tts.so）
pgrep -f '^/usr/bin/mico_aivs_lab$' | head -1 | xargs -I{} cat /proc/{}/environ | tr '\0' '\n' | grep LD_PRELOAD
```

## 自行构建（可选）

`hook_tts.c` 源码在库内，用 Mac clang 交叉编译（免 libc，armv7 ARM EABI5）：

```sh
clang --target=armv7-linux-gnueabihf -marm -nostdlib -fPIC -shared -O2 \
  -fno-stack-protector -fno-builtin -ffreestanding -fuse-ld=$PWD/.tools/ld.lld \
  speaker/hook/hook_tts.c -o speaker/hook/hook_tts.so
```

`.tools/ld.lld` 需符号链接 rust-lld（`~/.rustup/toolchains/stable-aarch64-apple-darwin/
lib/rustlib/aarch64-apple-darwin/bin/rust-lld`）且文件名含 ld.lld 才走 GNU 模式。

已知坑：

- 官方进程是 32 位 ARM EABI5（非 aarch64），ARM32 `O_DIRECTORY=0x4000`
- aarch64 内核 32 位兼容层 `time(13)` 系统调用不可用——取时间必须用
  `clock_gettime(263)` + `long ts[2]`
- LD_PRELOAD 链前列者先解析：hook_final 用 `dlsym(RTLD_NEXT)` 进入 hook_tts
- 测试钩子不能用 busybox 静态链接 syscall 直发（绕过 write 符号），要用
  stub 库方案编译动态引用 write 的测试程序

## 注意事项

- 二进制与音箱固件版本强相关（mico_aivs_lab 是 ARM EABI5 小端），**不要**用在
  其他架构的设备上
- 任何 `init.d restart`（migpt 的 abortXiaoAI 等）都会冲掉 LD_PRELOAD 注入，
  音箱端一律用 `restart-aivs.sh` 重启（带 hook）
