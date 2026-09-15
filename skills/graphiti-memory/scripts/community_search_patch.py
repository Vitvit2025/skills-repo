"""Поиск по СООБЩЕСТВАМ через штатный MCP-инструмент search_nodes (у MCP-сервера Graphiti 1.29 нет инструмента для
communities, а search_nodes ищет только :Entity). Патч: если search_nodes вызван с entity_types=["Community"],
Graphiti.search_ выполняет COMMUNITY_HYBRID_SEARCH_RRF (BM25 + косинус по name_embedding сообщества) и отдаёт
сообщества в поле nodes как EntityNode с labels=['Community'], summary = сводка сообщества,
attributes = {members: N, member_names: [до 20 имён карточек]}. Остальные вызовы не трогаются.
Подключается через sitecustomize (GRAPHITI_PATCH=1). Использование из Claude Code:
  mcp__graphiti__search_nodes(query="Алматы двойник", entity_types=["Community"], max_nodes=5)
"""
import logging

from graphiti_core import graphiti as _g
from graphiti_core.nodes import EntityNode
from graphiti_core.search.search_config import SearchConfig
from graphiti_core.search.search_config_recipes import COMMUNITY_HYBRID_SEARCH_RRF

log = logging.getLogger('community_search_patch')
_orig_search_ = _g.Graphiti.search_


async def _members(driver, uuid):
    try:
        rows, _, _ = await driver.execute_query(
            'MATCH (c:Community {uuid: $u})-[:HAS_MEMBER]->(m:Entity) '
            'RETURN count(m) AS n, collect(m.name)[0..20] AS names', u=uuid)
        if rows:
            return rows[0]['n'], rows[0]['names']
    except Exception as e:  # noqa: BLE001
        log.warning(f'members: {e!r}')
    return None, []


async def search_(self, query, config=None, group_ids=None, center_node_uuid=None,
                  bfs_origin_node_uuids=None, search_filter=None, driver=None):
    labels = getattr(search_filter, 'node_labels', None) or []
    if labels != ['Community']:
        kw = dict(group_ids=group_ids, center_node_uuid=center_node_uuid, bfs_origin_node_uuids=bfs_origin_node_uuids,
                  search_filter=search_filter, driver=driver)
        if config is not None:
            kw['config'] = config
        return await _orig_search_(self, query, **kw)
    limit = getattr(config, 'limit', 10) or 10
    cfg = SearchConfig(community_config=COMMUNITY_HYBRID_SEARCH_RRF.community_config, limit=max(limit, 25))
    try:
        res = await _orig_search_(self, query, config=cfg, group_ids=group_ids, driver=driver)
    except Exception as e:  # noqa: BLE001 — например, нет fulltext-индекса community_name → только косинус
        log.warning(f'community hybrid search failed ({e!r}), fallback to cosine only')
        from graphiti_core.search.search_config import CommunitySearchConfig, CommunitySearchMethod, CommunityReranker
        cfg = SearchConfig(community_config=CommunitySearchConfig(
            search_methods=[CommunitySearchMethod.cosine_similarity], reranker=CommunityReranker.rrf), limit=max(limit, 25))
        res = await _orig_search_(self, query, config=cfg, group_ids=group_ids, driver=driver)
    base_drv = driver or self.driver
    nodes = []
    for c in res.communities:
        # FalkorDB: граф = group_id; драйвер MCP по умолчанию смотрит в default_db → клонируем на граф сообщества
        drv = base_drv.clone(database=c.group_id) if hasattr(base_drv, 'clone') and c.group_id else base_drv
        n, names = await _members(drv, c.uuid)
        nodes.append(EntityNode(uuid=c.uuid, name=c.name, group_id=c.group_id, labels=['Community'],
                                created_at=c.created_at, summary=c.summary or '',
                                attributes={'members': n, 'member_names': names}))
    res.nodes = nodes
    res.node_reranker_scores = list(res.community_reranker_scores)
    return res


_g.Graphiti.search_ = search_
print('community_search_patch: search_nodes(entity_types=["Community"]) → поиск по сообществам', flush=True)
