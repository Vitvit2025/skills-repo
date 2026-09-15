"""Monkeypatch graphiti-core 0.30.x: поиск похожих узлов/рёбер в FalkorDB через ВЕКТОРНЫЙ ИНДЕКС (HNSW),
а не полным сканом всех эмбеддингов группы (vec.cosineDistance в WHERE — линейно по графу, 120–140 мс на запрос,
десятки запросов на эпизод при дедупе → батчи росли с 20 до 500 с).

В 0.30.x реальные реализации — функции-модули graphiti_core.search.search_utils.node_similarity_search /
edge_similarity_search (driver.search_interface по умолчанию None, класс FalkorSearchOperations в этом пути не участвует).
Они импортированы по имени в несколько модулей (search.search, utils.maintenance.node_operations, …) → подменяем
атрибут во ВСЕХ загруженных модулях graphiti_core, где лежит оригинал.

Индексы (Entity.name_embedding, RELATES_TO.fact_embedding) создаются лениво в графе, к которому привязан driver
(driver._database = группа; execute_query глотает «already indexed»). Для «простых» вызовов (без фильтров и без привязки
к конкретным узлам) кандидаты берутся из индекса (k = limit*OVERFETCH), далее те же group_id/min_score и тот же RETURN.
Ошибка → откат на штатный скан. Паттерн: JeremyErard/sdi-graphiti-service PR#58.
Статистика: falkor_vector_patch.stats.
"""
import collections, logging, os, sys
import graphiti_core.graphiti  # noqa: F401  — чтобы все модули с импортами по имени были загружены до подмены
import graphiti_core.search.search_utils as su
from graphiti_core.driver.driver import GraphProvider
from graphiti_core.driver.record_parsers import entity_edge_from_record, entity_node_from_record
from graphiti_core.graph_queries import get_vector_cosine_func_query
from graphiti_core.models.edges.edge_db_queries import get_entity_edge_return_query
from graphiti_core.models.nodes.node_db_queries import get_entity_node_return_query

log = logging.getLogger('falkor_vector_patch')
DIM = int(os.environ.get('EMBEDDER_DIMENSIONS', '1024'))
OVERFETCH = int(os.environ.get('VECTOR_OVERFETCH', '4'))
stats = collections.Counter()
_ensured: set = set()
_orig_node = su.node_similarity_search
_orig_edge = su.edge_similarity_search

INDEX_QUERIES = [
    "CREATE VECTOR INDEX FOR (n:Entity) ON (n.name_embedding) OPTIONS {dimension:%d, similarityFunction:'cosine'}" % DIM,
    "CREATE VECTOR INDEX FOR ()-[e:RELATES_TO]-() ON (e.fact_embedding) OPTIONS {dimension:%d, similarityFunction:'cosine'}" % DIM,
]


def _plain(search_filter) -> bool:
    try:
        return all(v in (None, [], {}, '') for v in search_filter.model_dump().values())
    except Exception:
        return False


def _usable(driver) -> bool:
    return getattr(driver, 'provider', None) == GraphProvider.FALKORDB


async def _ensure(driver):
    key = getattr(driver, '_database', None)
    if key in _ensured: return
    _ensured.add(key)
    for q in INDEX_QUERIES:
        try:
            await driver.execute_query(q)
            stats['index_create_attempt'] += 1
        except Exception as e:
            if 'already' not in str(e).lower(): log.warning('vector index %s: %s', key, str(e)[:160])


async def node_similarity_search(driver, search_vector, search_filter, group_ids=None,
                                 limit=su.RELEVANT_SCHEMA_LIMIT, min_score=su.DEFAULT_MIN_SCORE):
    if not _usable(driver) or not _plain(search_filter):
        stats['node_fallback_filtered'] += 1
        return await _orig_node(driver, search_vector, search_filter, group_ids, limit, min_score)
    await _ensure(driver)
    where = 'WITH n WHERE n.group_id IN $group_ids\n' if group_ids is not None else 'WITH n\n'
    cypher = (
        "CALL db.idx.vector.queryNodes('Entity', 'name_embedding', $k, vecf32($search_vector)) YIELD node AS n\n" + where +
        "WITH n, " + get_vector_cosine_func_query('n.name_embedding', '$search_vector', GraphProvider.FALKORDB) + " AS score\n"
        "WHERE score > $min_score\nRETURN " + get_entity_node_return_query(GraphProvider.FALKORDB) +
        "\nORDER BY score DESC LIMIT $limit"
    )
    try:
        params = dict(search_vector=search_vector, k=max(limit * OVERFETCH, limit), limit=limit, min_score=min_score)
        if group_ids is not None: params['group_ids'] = group_ids
        records, _, _ = await driver.execute_query(cypher, **params)
        stats['node_index'] += 1
        return [entity_node_from_record(r) for r in records]
    except Exception as e:
        stats['node_error'] += 1
        if stats['node_error'] <= 3: log.warning('node vector search → scan: %s', str(e)[:200])
        return await _orig_node(driver, search_vector, search_filter, group_ids, limit, min_score)


def _only_edge_uuids(search_filter):
    try:
        d = search_filter.model_dump()
        return bool(d.get('edge_uuids')) and all(v in (None, [], {}, '') for k, v in d.items() if k != 'edge_uuids')
    except Exception:
        return False


async def edge_similarity_search(driver, search_vector, source_node_uuid, target_node_uuid, search_filter,
                                 group_ids=None, limit=su.RELEVANT_SCHEMA_LIMIT, min_score=su.DEFAULT_MIN_SCORE):
    if _usable(driver) and source_node_uuid is None and target_node_uuid is None and _only_edge_uuids(search_filter):
        # фильтр только по списку uuid рёбер: штатный WHERE e.uuid IN $edge_uuids = скан всех рёбер (55–70 мс);
        # UNWIND + MATCH ()-[e {uuid}]->() БЕЗ меток на концах идёт по range-индексу (0.7 мс; с метками — 250 мс)
        where = 'WHERE e.group_id IN $group_ids\n' if group_ids is not None else ''
        cypher = (
            "UNWIND $edge_uuids AS u MATCH ()-[e:RELATES_TO {uuid: u}]->()\n"
            "WITH e, startNode(e) AS n, endNode(e) AS m\n" + where +
            "WITH DISTINCT e, n, m, " + get_vector_cosine_func_query('e.fact_embedding', '$search_vector', GraphProvider.FALKORDB) + " AS score\n"
            "WHERE score > $min_score\nRETURN " + get_entity_edge_return_query(GraphProvider.FALKORDB) +
            "\nORDER BY score DESC LIMIT $limit"
        )
        try:
            params = dict(search_vector=search_vector, limit=limit, min_score=min_score, edge_uuids=list(search_filter.edge_uuids))
            if group_ids is not None: params['group_ids'] = group_ids
            records, _, _ = await driver.execute_query(cypher, **params)
            stats['edge_by_uuids_fast'] += 1
            return [entity_edge_from_record(r) for r in records]
        except Exception as e:
            stats['edge_by_uuids_error'] += 1
            if stats['edge_by_uuids_error'] <= 3: log.warning('edge by uuids → scan: %s', str(e)[:200])
            return await _orig_edge(driver, search_vector, source_node_uuid, target_node_uuid, search_filter, group_ids, limit, min_score)
    if not _usable(driver) or source_node_uuid is not None or target_node_uuid is not None or not _plain(search_filter):
        stats['edge_fallback_filtered'] += 1
        return await _orig_edge(driver, search_vector, source_node_uuid, target_node_uuid, search_filter, group_ids, limit, min_score)
    await _ensure(driver)
    # Концы ребра — через startNode/endNode: связка MATCH (n)-[e {uuid: rel.uuid}]->(m) заставляет FalkorDB перебирать
    # ВСЕ карточки на каждого кандидата (замер на 2.9k фактов: 1640 мс против 1 мс; штатный скан 70 мс)
    where = 'WHERE e.group_id IN $group_ids\n' if group_ids is not None else ''
    cypher = (
        "CALL db.idx.vector.queryRelationships('RELATES_TO', 'fact_embedding', $k, vecf32($search_vector)) YIELD relationship AS e\n"
        "WITH e, startNode(e) AS n, endNode(e) AS m\n" + where +
        "WITH DISTINCT e, n, m, " + get_vector_cosine_func_query('e.fact_embedding', '$search_vector', GraphProvider.FALKORDB) + " AS score\n"
        "WHERE score > $min_score\nRETURN " + get_entity_edge_return_query(GraphProvider.FALKORDB) +
        "\nORDER BY score DESC LIMIT $limit"
    )
    try:
        params = dict(search_vector=search_vector, k=max(limit * OVERFETCH, limit), limit=limit, min_score=min_score)
        if group_ids is not None: params['group_ids'] = group_ids
        records, _, _ = await driver.execute_query(cypher, **params)
        stats['edge_index'] += 1
        return [entity_edge_from_record(r) for r in records]
    except Exception as e:
        stats['edge_error'] += 1
        if stats['edge_error'] <= 3: log.warning('edge vector search → scan: %s', str(e)[:200])
        return await _orig_edge(driver, search_vector, source_node_uuid, target_node_uuid, search_filter, group_ids, limit, min_score)


# Полнотекстовый поиск (второй источник кандидатов дедупа) в 0.30.1 иногда падает с «RediSearch: Syntax error … near X»
# (санитайзер не все спецсимволы убирает) и роняет ВЕСЬ батч. Ограждаем: ошибка → пустой список + лог (кандидаты
# всё равно придут из векторного поиска).
_orig_edge_ft = su.edge_fulltext_search
_orig_node_ft = su.node_fulltext_search


async def edge_fulltext_search(driver, query, search_filter, group_ids=None, limit=su.RELEVANT_SCHEMA_LIMIT):
    """Форма запроса из graphiti-core 0.30.2 (fix «avoid a full :Entity scan per hit»): концы ребра через
    startNode/endNode вместо MATCH (n)-[e {uuid: rel.uuid}]->(m). Замер на 2.9k фактов: 1040 мс → 1 мс."""
    if not _usable(driver):
        return await _orig_edge_ft(driver, query, search_filter, group_ids, limit)
    try:
        fuzzy = su.fulltext_query(query, group_ids, driver)
        if fuzzy == '': return []
        filter_queries, filter_params = su.edge_search_filter_query_constructor(search_filter, driver.provider)
        if group_ids is not None:
            filter_queries.append('e.group_id IN $group_ids'); filter_params['group_ids'] = group_ids
        cypher = (
            su.get_relationships_query('edge_name_and_fact', limit=limit, provider=driver.provider) +
            "\nYIELD relationship AS rel, score\nWITH rel AS e, score, startNode(rel) AS n, endNode(rel) AS m\n"
            "WHERE n:Entity AND m:Entity" + ''.join(f' AND {f}' for f in filter_queries) +
            "\nWITH e, score, n, m\nRETURN " + get_entity_edge_return_query(driver.provider) +
            "\nORDER BY score DESC LIMIT $limit"
        )
        records, _, _ = await driver.execute_query(cypher, query=fuzzy, limit=limit, routing_='r', **filter_params)
        stats['edge_fulltext_fast'] += 1
        return [su.get_entity_edge_from_record(r, driver.provider) for r in records]
    except Exception as e:
        stats['edge_fulltext_error'] += 1
        if stats['edge_fulltext_error'] <= 3: log.warning('edge fulltext → []: %s', str(e)[:200])
        return []


async def node_fulltext_search(*args, **kwargs):
    try:
        return await _orig_node_ft(*args, **kwargs)
    except Exception as e:
        stats['node_fulltext_error'] += 1
        if stats['node_fulltext_error'] <= 3: log.warning('node fulltext → []: %s', str(e)[:200])
        return []


patched_in = []
for name, mod in list(sys.modules.items()):
    if not name.startswith('graphiti_core') or mod is None: continue
    for attr, orig, new in (('node_similarity_search', _orig_node, node_similarity_search),
                            ('edge_similarity_search', _orig_edge, edge_similarity_search),
                            ('edge_fulltext_search', _orig_edge_ft, edge_fulltext_search),
                            ('node_fulltext_search', _orig_node_ft, node_fulltext_search)):
        if getattr(mod, attr, None) is orig:
            setattr(mod, attr, new); patched_in.append(f'{name}.{attr}')
print(f'falkor_vector_patch: dim={DIM} overfetch={OVERFETCH} подменено в {len(patched_in)} местах: {patched_in}', flush=True)
