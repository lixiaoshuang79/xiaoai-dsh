# probe/ — 刷机探针与工具

小爱音箱刷机（获取 root）建立在 [idootop/open-xiaoai](https://github.com/idootop/open-xiaoai)
的 USB 利用链之上。本目录提供：

| 文件 | 说明 |
|------|------|
| `flash` | macOS 刷机脚本（上游 del.wang 风格封装：connect/delay/switch/system 子命令，调用 macos/update 工具） |
| `usbprobe.c` | USB 探针诊断程序（libusb 枚举设备，用于定位刷机模式设备） |
| `usbif.c` | USB 接口辅助（探针依赖） |
| `usbprobe3.c` | 独立探针（写 /tmp/probe3.log 分步日志，排查 libusb 初始化/枚举卡点） |

## 使用

完整刷机步骤见 [docs/flashing.md](../../docs/flashing.md)。

```sh
# 编译探针（macOS，需 brew install libusb）
gcc -o usbprobe3 usbprobe3.c -lusb-1.0
./usbprobe3 && cat /tmp/probe3.log   # 看设备是否进入 USB 模式

# 刷机（音箱用数据线接 Mac 后）
./flash connect          # 进入刷机模式
./flash switch boot0     # 切 boot0
./flash system system0 <上游固件镜像>   # 刷入 root_patched 镜像
```

> 注意：上游固件镜像与 client 二进制不随本仓库分发（体积大 + 上游已停止维护），
> 请按 flashing.md 从 open-xiaoai 上游 releases 获取。
