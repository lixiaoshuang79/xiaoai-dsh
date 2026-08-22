---
name: recent-file-changes
description: 分析电脑上最近 N 天修改过的文件并按时间排序时
---

名称: recent-file-changes
何时使用: 分析电脑上最近 N 天修改过的文件并按时间排序时
步骤:
1. find ~/deepseek ~/Desktop ~/Documents ~/Downloads -type f -mtime -7 -not -path "*/node_modules/*" -not -path "*/.git/*" -not -path "*/__pycache__/*" 先限定常用目录并排除工程缓存
2. xargs stat -f '%m|%z|%Sm|%N' -t '%Y-%m-%d %H:%M' 后 sort -rn 按修改时间倒序排列
3. 按二级目录 awk 分组统计文件分布，识别改动热点（如日志/数据库类过程文件），给出清理建议
