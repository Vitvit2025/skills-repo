#!/bin/bash
# Сообщества графа: кластеризация карточек (label propagation + Лувен для гигантского кома) → сводка на кластер (модель) →
# узлы :Community + рёбра HAS_MEMBER. Старые сообщества графа сносятся и строятся заново (карточки/факты не трогаются).
#   scripts/communities.sh --plan     только кластеризация и оценка (без модели, без записи), состав → state/communities_plan.json
#   scripts/communities.sh            построить (main: 320 сообществ ≈ 6k коротких вызовов flash-lite, ~4 мин, <$1)
#   scripts/communities.sh --rename-only   срезать шаблонные префиксы имён + пересчитать эмбеддинг
# Поиск из сессии: search_nodes(query, entity_types=["Community"]) — патч community_search_patch.py (через sitecustomize).
# Крон: раз в неделю после ночной докачки (пример: 30 4 * * 0), лог communities.log.
. "$(dirname "$0")/lib.sh"
G=$GM_GRAPH; MODE=${1:---apply}
gm_log "сообщества $G ($MODE)"
gm_sync_loaders
$DEX /app/loaders/build_communities.py --group "$G" "$MODE" --concurrency "${CONCURRENCY:-8}" --max-size "${MAX_SIZE:-100}" --min-size "${MIN_SIZE:-3}" \
  --dump /app/tools/state/communities_plan.json 2>&1 | gm_filt_raw | sed -u 's/^/[communities] /'
gm_log "сообществ в $G: $(docker exec "$GM_CONTAINER" redis-cli GRAPH.QUERY "$G" "MATCH (c:Community) RETURN count(c)" 2>/dev/null | sed -n 2p)"
