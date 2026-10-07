#!/usr/bin/env bash
# 启动 PixelUI（带网页代理，Chrome 才能正常浏览网页）
cd "$(dirname "$0")"
PORT=${PORT:-8765}
(sleep 1; xdg-open "http://localhost:$PORT" >/dev/null 2>&1 || true) &
exec python3 serve.py
