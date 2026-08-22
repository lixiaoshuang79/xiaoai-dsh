---
name: mac-temp-cleanup
description: 用户要求清理电脑临时文件或缓存、释放磁盘空间时
---

名称: mac-temp-cleanup
何时使用: 用户要求清理电脑临时文件或缓存、释放磁盘空间时
步骤:
1. du -sh ~/Library/Caches /tmp /var/tmp 先查看各临时目录占用情况
2. find ~/Library/Caches -type f -mtime +7 -delete 删除超过 7 天未使用的缓存文件，再 find ~/Library/Caches -type d -empty -delete 清掉空目录
3. df -h / 和 du -sh ~/Library/Caches 验证释放空间与清理结果
