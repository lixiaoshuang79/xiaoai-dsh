#!/bin/sh
# hass-mcp 常驻服务包装脚本：从后台生成的 .env 读取 HA 地址/令牌，以 Streamable HTTP 模式运行
# 依赖：本目录下 .venv（pip install hass-mcp），
#       .env 由 localhost 后台生成：config/generated/bridge/.env（HA_URL + HA_TOKEN）。
cd "$(dirname "$0")" || exit 1
ENV_FILE=".env"
[ -f "$ENV_FILE" ] || ENV_FILE="../config/generated/bridge/.env"
export HA_URL=$(grep '^HA_URL=' "$ENV_FILE" | cut -d= -f2-)
export HA_TOKEN=$(grep '^HA_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
exec .venv/bin/hass-mcp --http --host 127.0.0.1 --port 8321
