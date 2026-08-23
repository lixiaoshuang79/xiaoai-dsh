# speaker/hook — 构建与可复现性说明（BUILD.md）

本目录维护两个 LD_PRELOAD 钩子二进制：`hook_final.so`（设备指令拦截，链首）与
`hook_tts.so`（官方 TTS/mediaplayer 拦截，链尾）。两者均为 **32 位 ARM EABI5**
（官方进程 `mico_aivs_lab` 是 armv7 小端 glibc 程序），产物只用于本音箱，
**不要**用在其他架构/固件上。

产物校验（sha256，2026-08-23 记录）：

| 文件 | sha256 |
|---|---|
| `hook_final.so` | `afb26c61ed40adcb9aee509e308d8e8de226a6fd7979da99d84df10a303f307f` |
| `hook_tts.so` | `fd5796054537f40a441fdc39eee32dfad9b8520bf4f9e03c525dd09ea11974cd` |

校验命令：

```sh
shasum -a 256 speaker/hook/hook_final.so speaker/hook/hook_tts.so
# 部署到音箱后与上表比对，不一致说明二进制被替换/损坏，立即停止使用
```

## hook_tts.so — 可复现交叉编译

源码 `hook_tts.c` 在库内，用 Mac clang 交叉编译（免 libc，armv7 ARM EABI5）：

```sh
# 1. 准备链接器：rust-lld 需以 ld.lld 名字存在才走 GNU 模式
RUST_LDDIR=~/.rustup/toolchains/stable-aarch64-apple-darwin/lib/rustlib/aarch64-apple-darwin/bin
mkdir -p .tools
ln -sf "$RUST_LDDIR/rust-lld" .tools/ld.lld

# 2. 编译（与仓库产物一致的命令）
clang --target=armv7-linux-gnueabihf -marm -nostdlib -fPIC -shared -O2 \
  -fno-stack-protector -fno-builtin -ffreestanding -fuse-ld=$PWD/.tools/ld.lld \
  speaker/hook/hook_tts.c -o speaker/hook/hook_tts.so

# 3. 校验产物
file speaker/hook/hook_tts.so          # 应输出 ELF 32-bit LSB shared object, ARM, EABI5
shasum -a 256 speaker/hook/hook_tts.so # 应与上表一致（同一编译器版本下）
```

已知坑（详见 README.md）：

- 官方进程是 32 位 ARM EABI5（非 aarch64），ARM32 `O_DIRECTORY=0x4000`（非 64 位 `0x10000`）
- aarch64 内核 32 位兼容层 `time(13)` 系统调用不可用——取时间必须用
  `clock_gettime(263)` + `long ts[2]`
- 音箱是 glibc（非 musl），LD_PRELOAD 链前列者先解析：
  `hook_final.so:hook_tts.so`（hook_final 用 `dlsym(RTLD_NEXT)` 进入 hook_tts）
- 测试钩子不能用 busybox 静态链接 syscall 直发（绕过 write 符号），要用
  stub 库方案编译动态引用 write 的测试程序（`-L. -lstubwrite` + `--dynamic-linker=/lib/ld-linux-armhf.so.3`）

## hook_final.so — 来源说明（无源码）

`hook_final.so` **在仓库中没有对应源码**。它由上游 open-xiaoai 生态/现网部署
提供（词表编译在二进制内，`strings` 不可见，无法从仓库源码复现），仓库仅保留
二进制供音箱直接使用。

因此：

1. **不可从本仓库复现构建**——不要尝试反编译/伪造，也不要删除二进制
   （音箱部署依赖它）；
2. **建议安全审计后替换**：如果希望完全可审计，请基于 `hook_tts.c` 的
   开发模式自行实现设备词拦截钩子（写 `write()` 钩子 + 词表匹配 +
   击杀宿主进程），源码进库后即可复现构建与审计；
3. 部署/验证命令（与 README.md 一致）：

   ```sh
   # 音箱上（经 SSH 或 migpt /exec 端点）
   cp hook_final.so hook_tts.so /data/open-xiaoai/
   /data/open-xiaoai/restart-aivs.sh /data/open-xiaoai/hook_final.so
   # 验证注入：主进程 environ 里应有 LD_PRELOAD（链顺序 hook_final.so:hook_tts.so）
   pgrep -f '^/usr/bin/mico_aivs_lab$' | head -1 | xargs -I{} cat /proc/{}/environ | tr '\0' '\n' | grep LD_PRELOAD
   ```

4. 任何 `init.d restart`（migpt 的 abortXiaoAI 等）都会冲掉 LD_PRELOAD 注入，
   音箱端一律用 `restart-aivs.sh` 重启（带 hook）。
