---
name: dingtalk-query
description: 用户问钉钉（有没有人找我/查消息/查邮件/查日程/查同事等）时，用 dws CLI 查询
---

名称: dingtalk-query
何时使用: 用户问任何钉钉相关的事——「钉钉上有没有人找过我」「XX 给我发消息了吗」
「查一下钉钉邮件」「看看今天的日程」「找一下 XX 同事」等。这是「钉钉数据查询」的通用链路。

步骤:
1. 用 dws CLI（钉钉官方 Workspace CLI）查询，账号由部署者自行登录（dws auth login --device 扫码），
   直接查，不要问用户要账号/密码。
   **dws 用绝对路径**（`which dws` 查；launchd 常驻进程 PATH 不含 ~/.local/bin 时要用全路径）。
2. 常用查询命令:
   - 有没有人找过我 / 最近消息:
     dws chat +conversation-list --page-all --format json   # 会话列表
     dws chat +chat-messages --open-conversation-id <cid> --limit 5 --format json  # 读会话最近消息
     判断「别人找过我」: 消息的 sender 不是自己（自己的 senderId 要先用
     dws contact user get --ids <自己的userId> 查出来比对，或看 sender 显示名）
   - 查邮件: dws mail +list 或 dws mail --help 看子命令
   - 查日程: dws calendar --help 看子命令
   - 找同事/查通讯录: dws contact user search --query "姓名" / dws aisearch person --query "线索"
   - 查知识库/文档: dws aisearch enterprise --query "关键词"
3. 结果整理成口语短句播报（挑重点，别念全文）: 谁、什么时候、说了什么。
   没消息就如实说「最近没有人找您」。
4. 查询命令不确定时，先跑 dws <产品> --help 或看 ~/.agents/skills/dingtalk-<产品>/SKILL.md
   （钉钉官方技能，13 个: chat/mail/calendar/contact/aisearch/wiki/doc/drive/todo/minutes/...）
注意事项:
- dws 输出用 --format json，按真实返回字段解析，不猜字段名
- 时间戳转成「8月21日 上午11点」这样的口语表达
- 只读查询随便查；发消息/发邮件等写操作先跟用户确认再执行
- 登录态失效（报认证错误）时如实告知用户需要重新扫码授权，不要反复重试
- 音箱侧深通道用本技能；Mac 主 DSH 会话直接用钉钉官方技能 dingtalk-*
