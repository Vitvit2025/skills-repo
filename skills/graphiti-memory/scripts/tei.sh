#!/bin/bash
# Локальный эмбеддер (TEI на CPU) для MCP-сервера — embedder.local_tei из конфига. Массовую загрузку он НЕ тянет
# (очередь 3–4 с, 429), для неё эмбеддинги идут в облако (embedder.bulk_api_url); векторы у bge-m3 идентичны (cos 1.0000).
# Слушает 127.0.0.1:<port> и на адресе docker-моста (172.17.0.1) — чтобы контейнер graphiti-mcp дотянулся.
. "$(dirname "$0")/lib.sh"
g() { python3 "$GM_SCRIPTS/gm_config.py" get "embedder.local_tei.$1"; }
[ "$(g enabled)" = "True" ] || { echo "embedder.local_tei.enabled != true — пропуск"; exit 0; }
NAME=$(g container); NAME=${NAME:-graphiti-embed}; PORT=$(g port); PORT=${PORT:-18081}
DATA=$GM_DEPLOY_DIR/$(g data_dir); mkdir -p "$DATA"
BRIDGE=$(ip -4 addr show docker0 2>/dev/null | grep -oP 'inet \K[\d.]+'); BRIDGE=${BRIDGE:-172.17.0.1}
docker rm -f "$NAME" >/dev/null 2>&1
docker run -d --name "$NAME" --restart unless-stopped --cpus "$(g cpus)" --memory "$(g memory)" \
  -p "127.0.0.1:$PORT:80" -p "$BRIDGE:$PORT:80" -v "$DATA:/data" "$(g image)" \
  --model-id "$(python3 "$GM_SCRIPTS/gm_config.py" get embedder.model)" --max-batch-tokens 2048 --max-client-batch-size 32 --max-concurrent-requests 256 --auto-truncate
echo "TEI $NAME: 127.0.0.1:$PORT и $BRIDGE:$PORT (первый старт качает модель — несколько минут). Проверка:"
echo "  curl -s http://127.0.0.1:$PORT/v1/embeddings -H 'Content-Type: application/json' -d '{\"input\":\"тест\"}' | head -c 100"
