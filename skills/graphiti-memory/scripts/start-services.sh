#!/bin/bash
set -e

# Start FalkorDB in background using the correct module path
echo "Starting FalkorDB..."
redis-server \
  --loadmodule /var/lib/falkordb/bin/falkordb.so \
  --protected-mode no \
  --bind 0.0.0.0 \
  --port 6379 \
  --dir /var/lib/falkordb/data \
  --daemonize yes

# Wait for FalkorDB to be ready
echo "Waiting for FalkorDB to be ready..."
# 🔴 наш фикс (15.09.2026): при большом RDB redis-cli ping отвечает LOADING (код 0) → MCP стартовал раньше базы, падал,
# контейнер уходил в цикл рестартов и загрузка начиналась заново. Ждём именно PONG.
until [ "$(redis-cli -h localhost -p 6379 ping 2>/dev/null)" = "PONG" ]; do
  echo "FalkorDB not ready yet, waiting..."
  sleep 1
done
echo "FalkorDB is ready!"

# Start FalkorDB Browser if enabled (default: enabled)
if [ "${BROWSER:-1}" = "1" ]; then
  if [ -d "/var/lib/falkordb/browser" ] && [ -f "/var/lib/falkordb/browser/server.js" ]; then
    echo "Starting FalkorDB Browser on port 3000..."
    cd /var/lib/falkordb/browser
    HOSTNAME="0.0.0.0" node server.js > /var/log/graphiti/browser.log 2>&1 &
    echo "FalkorDB Browser started in background"
  else
    echo "Warning: FalkorDB Browser files not found, skipping browser startup"
  fi
else
  echo "FalkorDB Browser disabled (BROWSER=${BROWSER})"
fi

# Start MCP server in foreground
echo "Starting MCP server..."
cd /app/mcp
exec /root/.local/bin/uv run --no-sync main.py
