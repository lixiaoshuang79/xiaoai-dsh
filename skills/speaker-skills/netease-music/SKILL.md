---
name: netease-music
description: 网易云音乐（音箱版）。Use when 用户要点歌、放每日推荐、放红心歌单、放我的歌单、查歌词，或快速通道的 netease_music_play 等工具失败降级到深通道时。CLI 为 netease-music（node 脚本，自备网易云账号 cookie）。
---

# 网易云音乐（音箱深通道版）

用 netease-music CLI 完成点歌/推荐/歌单/歌词，拿播放直链后用音箱播放端点播放。

## 前置

- CLI：`netease-music`（node 脚本；账号 cookie 由 CLI 首次使用时配置维护，VIP 账号可拿无损音质）
- 播放端点：`http://127.0.0.1:4398/play_url`（POST JSON `{"url": "..."}`，音箱 miplayer 直接拉流，网易云 URL 裸拉可行）

## 命令

```bash
N=netease-music
$N search "周杰伦 七里香" --limit 5     # 搜索：{songs:[{id,name,artists,album,duration,fee}]}
$N daily --limit 5                     # 每日推荐 {songs:[...]}
$N liked --limit 10                    # 红心 {songs:[...]}
$N playlists                           # 歌单 {playlists:[{id,name,count,liked}]}
$N playlist <id> --limit 20            # 歌单歌曲
$N url <songId> --level lossless       # 播放直链 {url, level, freeTrial}（VIP 可无损）
$N lyric <songId>                      # 歌词 {lyric}
$N whoami                              # 登录态验证 {uid,nickname,vip}
$N --status                            # 风控状态（risk_level≥2 停用当天）
```

## 播放流程

1. 搜索或列表选歌 → 2. `$N url <id> --level lossless` 拿直链 → 3. `curl -X POST http://127.0.0.1:4398/play_url -d '{"url":"<url>"}'`
4. 把「歌名 - 歌手」写进最终回答（桥会润色播报）。

## 反封号红线（违反会封号，必须遵守）

- CLI 内置 ≥5s 请求间隔（5-9s 抖动），**禁止 NETEASE_NO_WAIT=1**
- 一次任务 ≤5-8 次调用；禁止并发跑多个 CLI 进程；禁止 for 循环批量拉
- `--status` risk_level≥2 → 当天不再调用，改用 web-audio-play 技能
- URL 时效约 20 分钟，过期重取

## 坑

- artists 字段是字符串（"周杰伦/方文山"，用 / 分隔），不是数组
- 歌词 LRC 每行带 [mm:ss.xx] 时间戳，要剥掉再念
- 搜索可能混入翻唱，优先选 fee=0/VIP 正版或大牌歌手结果
- 无版权歌 url 为 null（freeTrial 试听只有 60s 片段，别播）
- 歌单列表字段是 count 不是 trackCount
