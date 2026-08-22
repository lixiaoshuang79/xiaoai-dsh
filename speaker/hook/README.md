# speaker/hook — 零竞态拦截器（hook_final.so）

`hook_final.so` 是预编译的 ARM LD_PRELOAD 共享库（6504 字节），注入到音箱端
`/usr/bin/mico_aivs_lab` 进程，拦截**设备指令词**写入 instruction.log 的 `write()` 调用
瞬间击杀官方进程——云端收不到设备执行请求（零竞态，比脚本轮询日志快一个数量级）。

## 工作原理

1. `restart-aivs.sh <hook路径>` 通过 procd 的 `service set` 给 mico_aivs_lab 注入
   `LD_PRELOAD=/data/open-xiaoai/hook_final.so` 环境变量，再干净重启该进程
2. mico_aivs_lab 每次向 `/tmp/mico_aivs_lab/instruction.log` 写
   `RecognizeResult`（含设备指令词：开/关/调/灯/空调/窗帘…）时，hook 先检查内容：
   - 命中设备词 → 立即击杀宿主进程（官方云端下发的执行指令无人接收）
   - 未命中 → 放行原 write
3. procd 不会自动拉起被杀进程，`native-block.sh` 随后用 `restart-aivs.sh`
   干净重启并保持注入（钩子不丢）

## 部署

```sh
# 音箱上（经 SSH 或 migpt /exec 端点）
cp hook_final.so /data/open-xiaoai/hook_final.so
/data/open-xiaoai/restart-aivs.sh /data/open-xiaoai/hook_final.so
# 验证注入：主进程 environ 里应有 LD_PRELOAD
pgrep -f '^/usr/bin/mico_aivs_lab$' | head -1 | xargs -I{} cat /proc/{}/environ | tr '\0' '\n' | grep LD_PRELOAD
```

## 自行构建（可选）

本仓库以现网验证过的稳定二进制为唯一事实来源（词表编译在二进制内，strings 不可见）。
若需扩展设备词表或适配新固件，可参考 [docs/architecture.md](../../docs/architecture.md)
中音箱端拦截架构一节自行实现 LD_PRELOAD 钩子（拦截 `write()` → 匹配词表 → kill 宿主），
交叉编译目标 aarch64-linux-gnueabihf（Zig 0.16+ / musl 工具链均可）。

## 注意事项

- 该二进制与音箱固件版本强相关（mico_aivs_lab 是 ARM EABI5 小端），**不要**用在
  其他架构的设备上
- 任何 `init.d restart`（migpt 的 abortXiaoAI 等）都会冲掉 LD_PRELOAD 注入，
  音箱端一律用 `restart-aivs.sh` 重启（带 hook）
