"""Автозагрузка наших патчей в ЛЮБОЙ python-процесс контейнера (в т.ч. MCP-сервер), если PYTHONPATH=/app/loaders и GRAPHITI_PATCH=1.
Без патча MCP-сервер на большом графе ищет полным сканом (vec.cosineDistance + fulltext-связка 0.30.1 = перебор карточек
на каждого кандидата) → запрос на 30k фактов висел минуты и вешал FalkorDB (15.09.2026 19:00). Патчи те же, что у загрузчиков.
16.09.2026: + prompt_patch (промпты дедупа: уточнение ≠ противоречие, однофамильцы не склеивать) + small_model_patch (SMALL_MODEL_NAME)."""
import os
if os.environ.get('GRAPHITI_PATCH', '0') == '1':
    for _mod in ('embed_chunk_patch',      # ≤32 текстов на запрос к TEI
                 'falkor_vector_patch',    # HNSW-индекс вместо скана, быстрый fulltext/uuid-поиск фактов
                 'community_search_patch', # search_nodes(entity_types=["Community"]) ищет по сообществам
                 'prompt_patch',           # промпты дедупа сущностей/фактов
                 'small_model_patch'):     # умная модель на дедупе (SMALL_MODEL_NAME) и для MCP-пути
        try:
            __import__(_mod)
        except Exception as e:  # никогда не ронять процесс из-за патча
            print(f'sitecustomize: патч {_mod} не применён: {e!r}', flush=True)
