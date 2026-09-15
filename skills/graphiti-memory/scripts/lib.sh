#!/bin/bash
# Общая обвязка bash-скриптов скилла graphiti-memory: читает graphiti-memory.yaml через gm_config.py,
# подхватывает ключ API из env-файла, даёт функции docker exec / склейки / чистки.
#   . "$(dirname "$0")/lib.sh"            (конфиг: $GRAPHITI_MEMORY_CONFIG, иначе ../conf/graphiti-memory.yaml рядом со scripts/)
GM_SCRIPTS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
eval "$(python3 "$GM_SCRIPTS/gm_config.py" shell ${GRAPHITI_MEMORY_CONFIG:+--config "$GRAPHITI_MEMORY_CONFIG"})" || { echo "gm_config: конфиг не прочитан" >&2; exit 1; }
cd "$GM_DEPLOY_DIR" || exit 1
if [ -f "$GM_ENV_FILE" ]; then set -a; . "$GM_ENV_FILE"; set +a; fi
GM_KEY=${!GM_KEY_VAR:-}
[ -n "$GM_KEY" ] || echo "!! $GM_KEY_VAR пуст (env-файл $GM_ENV_FILE) — загрузчики не смогут работать" >&2
# запуск python внутри контейнера с полным набором env загрузчика (ключ — только здесь, в лог не печатать)
DEX="docker exec $GM_DOCKER_ENV -e OPENAI_API_KEY=$GM_KEY $GM_CONTAINER $GM_PY"
gm_log()  { echo "=== $(date -u +%FT%TZ) $*"; }
gm_filt() { grep --line-buffered -vE "$GM_LOG_FILTER" | sed -u "s/^/[$1] /"; }   # предупреждения graphiti — не ошибки
gm_sync_loaders() {
  # Скрипты и конфиг внутрь контейнера. Если каталоги смонтированы (compose из скилла) — ничего копировать не надо;
  # иначе (старый контейнер без монтирования) — кладём через `cat >` (docker cp в этот образ падает с «mkdirat … file exists»).
  local mounts; mounts=$(docker inspect -f '{{range .Mounts}}{{.Destination}} {{end}}' "$GM_CONTAINER" 2>/dev/null)
  docker exec "$GM_CONTAINER" mkdir -p /app/loaders /app/conf /data/sessions_filtered /app/tools/state >/dev/null 2>&1
  if [[ " $mounts " != *" /app/loaders "* ]]; then
    for f in gm_config.py bulk_load.py bulk_load_sessions.py falkor_vector_patch.py embed_chunk_patch.py merge_aliases.py; do
      docker exec -i "$GM_CONTAINER" sh -c "cat > /app/loaders/$f" < "$GM_SCRIPTS/$f"; done
    docker exec "$GM_CONTAINER" sh -c "rm -rf /app/loaders/__pycache__"
  fi
  if [[ " $mounts " != *" /app/conf "* ]]; then docker exec -i "$GM_CONTAINER" sh -c "cat > /app/conf/graphiti-memory.yaml" < "$GM_CONFIG"; fi
}
gm_push() {  # файл эпизодов → /data/sessions_filtered (если каталог не смонтирован)
  local mounts; mounts=$(docker inspect -f '{{range .Mounts}}{{.Destination}} {{end}}' "$GM_CONTAINER" 2>/dev/null)
  [[ " $mounts " == *" /data/sessions_filtered "* ]] || docker exec -i "$GM_CONTAINER" sh -c "cat > /data/sessions_filtered/$(basename "$1")" < "$1"
}
gm_merge() { gm_log "склейка псевдонимов ($1)"; $DEX /app/loaders/merge_aliases.py --graph "$GM_GRAPH" --apply 2>&1 | grep -E "готово|Traceback|Error" | sed -u 's/^/[merge] /'; }
gm_scrub() { "$GM_HOST_PY" "$GM_SCRIPTS/scrub_graph.py" --graphs "$GM_GRAPH" --apply 2>&1; }
gm_counts() {  # карточек / фактов / эпизодов графа
  local R="docker exec $GM_CONTAINER redis-cli"
  echo "карточек $($R GRAPH.QUERY "$GM_GRAPH" 'MATCH (n:Entity) RETURN count(n)' | sed -n 2p), фактов $($R GRAPH.QUERY "$GM_GRAPH" 'MATCH ()-[r:RELATES_TO]->() RETURN count(r)' | sed -n 2p), эпизодов $($R GRAPH.QUERY "$GM_GRAPH" 'MATCH (n:Episodic) RETURN count(n)' | sed -n 2p), state $(python3 -c "import json;print(len(json.load(open('$GM_STATE_DIR/$GM_GRAPH.json'))))" 2>/dev/null || echo 0)"
}
gm_alert() {  # Telegram владельцу (только при ошибках/секретах); токен из alerts.env_file
  [ -n "$GM_ALERT_ENV" ] && [ -f "$GM_ALERT_ENV" ] || return 0
  local tok chat; tok=$(grep -E "^${GM_ALERT_TOKEN_VAR}=" "$GM_ALERT_ENV" | cut -d= -f2- | tr -d '"'); chat=$(grep -E "^${GM_ALERT_CHAT_VAR}=" "$GM_ALERT_ENV" | cut -d= -f2- | tr -d '"')
  [ -n "$tok" ] && [ -n "$chat" ] && curl -s -m 20 "https://api.telegram.org/bot${tok}/sendMessage" -d chat_id="$chat" --data-urlencode "text=$1" >/dev/null 2>&1 || true
}
