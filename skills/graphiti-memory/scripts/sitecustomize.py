"""Автозагрузка наших патчей в ЛЮБОЙ python-процесс контейнера (в т.ч. MCP-сервер), если PYTHONPATH=/app/loaders и GRAPHITI_PATCH=1.
Без патча MCP-сервер на большом графе ищет полным сканом (vec.cosineDistance + fulltext-связка 0.30.1 = перебор карточек
на каждого кандидата) → запрос на 30k фактов висел минуты и вешал FalkorDB (15.09.2026 19:00). Патчи те же, что у загрузчиков."""
import os
if os.environ.get('GRAPHITI_PATCH', '0') == '1':
    try:
        import embed_chunk_patch  # noqa: F401  — ≤32 текстов на запрос к TEI
        import falkor_vector_patch  # noqa: F401  — HNSW-индекс вместо скана, быстрый fulltext/uuid-поиск фактов
    except Exception as e:  # никогда не ронять процесс из-за патча
        print(f'sitecustomize: патч не применён: {e!r}', flush=True)
