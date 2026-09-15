#!/bin/bash
# Состояние графовой памяти: контейнеры, граф (карточки/факты/эпизоды, доля invalid_at, типы), state, MCP, последняя докачка.
. "$(dirname "$0")/lib.sh"
R="docker exec $GM_CONTAINER redis-cli"; G=$GM_GRAPH
echo "== контейнеры"; docker ps --format '  {{.Names}}  {{.Status}}  {{.Ports}}' | grep -E "$GM_CONTAINER|graphiti|embed" || echo "  $GM_CONTAINER НЕ запущен"
echo "== графы FalkorDB: $($R GRAPH.LIST | tr '\n' ' ')"
echo "== граф $G: $(gm_counts)"
echo "   фактов с invalid_at: $($R GRAPH.QUERY "$G" 'MATCH ()-[r:RELATES_TO]->() WHERE r.invalid_at IS NOT NULL RETURN count(r)' | sed -n 2p)"
echo "   карточек по типам:"; $R GRAPH.QUERY "$G" 'MATCH (n:Entity) RETURN labels(n), count(n) ORDER BY count(n) DESC' | grep -v -E "^(labels|count|Cached|Query)" | paste - - | sed 's/^/     /'
echo "   векторные индексы: $($R GRAPH.QUERY "$G" 'CALL db.indexes() YIELD types RETURN types' | grep -c VECTOR) (ожидается 2)"
echo "   рестартов контейнера: $(docker inspect -f '{{.RestartCount}}' "$GM_CONTAINER" 2>/dev/null) (>0 = см. runbook «цикл рестартов»); патч в MCP: $(docker logs "$GM_CONTAINER" 2>&1 | grep -a -c 'falkor_vector_patch: dim=')×"
echo "== MCP: $(python3 "$GM_SCRIPTS/mcp_client.py" get_status '{}' 2>/dev/null | head -c 200 || echo 'недоступен')"
echo "== последний снимок: $(ls -1t "$GM_BACKUP_DIR"/dump-*.rdb 2>/dev/null | head -1 || echo 'нет (scripts/backup.sh)')"
echo "== последняя докачка: $(grep -E "Graphiti-докачка" "$GM_DEPLOY_DIR/cron_load.log" 2>/dev/null | tail -1)"
echo "== крон: $(crontab -l 2>/dev/null | grep -E "cron_load.sh" || echo 'не установлен')"
