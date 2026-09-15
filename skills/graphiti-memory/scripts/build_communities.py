#!/usr/bin/env python3
"""Сообщества (communities) для графа Graphiti: кластеризация карточек (label propagation, без модели) →
сводка на каждое сообщество (модель, попарное слияние сводок участников) → узлы :Community + рёбра HAS_MEMBER.

Зачем свой скрипт, а не MCP-инструмент build_communities: (1) MCP-вызов синхронный и на 7.7k карточек висит десятки минут
(HTTP-таймаут, риск повесить MCP); (2) штатный код делает по LLM-вызову даже на кластер из ОДНОЙ карточки (913 изолированных)
и одну гигантскую сводку на «ядро» графа (хаб «Владелец»/прод: степень до 5.6k) — мы отсекаем мелкие кластеры (--min-size)
и режем гигантские на под-кластеры (--max-size, повторный label propagation по подграфу).

Режимы:  --plan   только кластеризация, гистограмма размеров, оценка вызовов/стоимости, ничего не пишет.
         --apply  сносит старые :Community группы и строит заново.
Запуск (в контейнере, как bulk_load): docker exec -e OPENAI_API_KEY=… -e OPENAI_API_URL=https://openrouter.ai/api/v1
  -e MODEL_NAME=google/gemini-2.5-flash-lite -e EMBEDDER_MODEL=BAAI/bge-m3 -e EMBEDDER_DIMENSIONS=1024
  -e EMBEDDER_API_URL=https://openrouter.ai/api/v1 graphiti-mcp /app/mcp/.venv/bin/python /app/loaders/build_communities.py --group main --plan
"""
import argparse, asyncio, json, os, sys, time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bulk_load import make_clients  # noqa: E402

from falkordb.asyncio import FalkorDB  # noqa: E402
from graphiti_core.driver.falkordb_driver import FalkorDriver  # noqa: E402
from graphiti_core.helpers import semaphore_gather  # noqa: E402
from graphiti_core.nodes import EntityNode  # noqa: E402
from graphiti_core.utils.maintenance import community_operations as co  # noqa: E402


async def projection_for_group(driver, group_id):
    """Одним запросом: карточка → соседи с числом рёбер (штатный код делает 7.7k запросов по одной карточке)."""
    records, _, _ = await driver.execute_query(
        """
        MATCH (n:Entity {group_id: $group_id})-[e:RELATES_TO]-(m:Entity {group_id: $group_id})
        WITH n.uuid AS src, m.uuid AS dst, count(e) AS cnt
        RETURN src, dst, cnt
        """,
        group_id=group_id,
    )
    proj = defaultdict(list)
    for r in records:
        proj[r['src']].append(co.Neighbor(node_uuid=r['dst'], edge_count=r['cnt']))
    all_uuids, _, _ = await driver.execute_query(
        'MATCH (n:Entity {group_id: $group_id}) RETURN n.uuid AS uuid', group_id=group_id)
    for r in all_uuids:
        proj.setdefault(r['uuid'], [])
    return proj


def label_propagation(proj, max_iter=30, seed=42):
    """Свой label propagation: асинхронные обновления в случайном порядке + предел итераций.
    Штатный co.label_propagation (синхронный, без предела) на нашем графе колеблется бесконечно (15.09: 5+ мин на 100 % CPU)."""
    import random
    rnd = random.Random(seed)
    label = {u: i for i, u in enumerate(proj)}
    order = list(proj)
    for it in range(max_iter):
        rnd.shuffle(order)
        changed = 0
        for u in order:
            nb = proj[u]
            if not nb:
                continue
            votes = defaultdict(int)
            for n in nb:
                votes[label[n.node_uuid]] += n.edge_count
            best = max(votes.values())
            cands = [l for l, v in votes.items() if v == best]
            if label[u] in cands:
                continue
            new = min(cands)
            if new != label[u]:
                label[u] = new; changed += 1
        if changed == 0:
            break
    groups = defaultdict(list)
    for u, l in label.items():
        groups[l].append(u)
    return list(groups.values()), it + 1, changed


def split_large(cluster, proj, max_size, depth=0):
    """Гигантский кластер (label propagation стягивает 80 % графа в один ком через хабы «Владелец»/прод) →
    Лувен (модулярность, networkx) по подграфу, рекурсивно с растущим resolution, пока куски не станут ≤ max_size.
    Если Лувен не делит — «снимаем хабы»: 5 % карточек с наибольшей степенью в отдельный кластер, остальное заново."""
    if len(cluster) <= max_size or depth > 6:
        return [cluster]
    import networkx as nx
    members = set(cluster)
    G = nx.Graph()
    G.add_nodes_from(cluster)
    for u in cluster:
        for n in proj.get(u, []):
            if n.node_uuid in members and u < n.node_uuid:
                G.add_edge(u, n.node_uuid, weight=n.edge_count)
    parts = [list(c) for c in nx.community.louvain_communities(G, weight='weight', resolution=1.0 + 0.5 * depth, seed=42)]
    if len(parts) <= 1:
        deg = sorted(cluster, key=lambda u: -G.degree(u, weight='weight'))
        hubs = deg[: max(1, len(cluster) // 20)]
        hub_set = set(hubs)
        rest = [u for u in cluster if u not in hub_set]
        parts = [hubs] + [list(c) for c in nx.community.louvain_communities(G.subgraph(rest), weight='weight', seed=42)]
    out = []
    for p in parts:
        out.extend(split_large(p, proj, max_size, depth + 1))
    return out


import re

_PREFIX = re.compile(r'^\s*(this|the)\s+(summary|text|document|passage|content)\s+'
                     r'(details|outlines|describes|covers|discusses|summarizes|provides|presents|explains|focuses on|'
                     r'is about|documents|highlights|addresses|concerns)\s+(the\s+)?', re.I)


def clean_name(name: str) -> str:
    """«This summary details the technical configuration…» → «Technical configuration…» (имя = BM25-ключ поиска)."""
    n = _PREFIX.sub('', name or '').strip()
    return (n[:1].upper() + n[1:]) if n else (name or '')


async def rename_pass(driver, embedder, group):
    """Пост-проход: срезать шаблонный префикс у имён сообществ и пересчитать name_embedding."""
    rows, _, _ = await driver.execute_query('MATCH (c:Community {group_id: $g}) RETURN c.uuid AS u, c.name AS n', g=group)
    todo = [(r['u'], r['n'], clean_name(r['n'])) for r in rows]
    todo = [t for t in todo if t[2] != t[1]]
    print(f'[{group}] переименовать сообществ: {len(todo)} из {len(rows)}', flush=True)
    for u, old, new in todo:
        emb = await embedder.create(input_data=[new])
        await driver.execute_query('MATCH (c:Community {uuid: $u}) SET c.name = $n, c.name_embedding = vecf32($e)',
                                   u=u, n=new, e=emb)
    return len(todo)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--group', default='main')
    ap.add_argument('--plan', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--rename-only', action='store_true', help='только чистка имён существующих сообществ + пересчёт эмбеддинга')
    ap.add_argument('--min-size', type=int, default=3, help='кластеры меньше — без сводки (не пишутся)')
    ap.add_argument('--max-size', type=int, default=100, help='кластеры больше — делятся на под-кластеры (Лувен)')
    ap.add_argument('--concurrency', type=int, default=8, help='сколько сообществ строить параллельно')
    ap.add_argument('--dump', default='', help='json с составом кластеров (для отладки)')
    a = ap.parse_args()
    if not (a.plan or a.apply or a.rename_only):
        ap.error('нужен --plan, --apply или --rename-only')

    fdb = FalkorDB(host=os.environ.get('FALKORDB_HOST', 'localhost'), port=6379,
                   max_connections=int(os.environ.get('FALKOR_MAX_CONN', '512')))
    driver = FalkorDriver(falkor_db=fdb, database=a.group)

    if a.rename_only:
        api_key = os.environ['OPENAI_API_KEY']; base = os.environ.get('OPENAI_API_URL', 'https://openrouter.ai/api/v1')
        _, embedder, _ = make_clients(api_key, base, os.environ.get('MODEL_NAME', 'google/gemini-2.5-flash-lite'),
                                      os.environ.get('EMBEDDER_MODEL', 'BAAI/bge-m3'), int(os.environ.get('EMBEDDER_DIMENSIONS', '1024')),
                                      os.environ.get('EMBEDDER_API_URL', base))
        await rename_pass(driver, embedder, a.group)
        await driver.close()
        return

    t0 = time.time()
    proj = await projection_for_group(driver, a.group)
    t_proj = time.time() - t0
    clusters, iters, last_changed = label_propagation(proj)
    print(f'[{a.group}] карточек={len(proj)} проекция {t_proj:.1f} с; кластеров (сырых)={len(clusters)} '
          f'итераций={iters} (изменений на последней {last_changed}) всего {time.time()-t0:.1f} с', flush=True)
    top = sorted(clusters, key=len, reverse=True)[:8]
    print('  крупнейшие сырые:', [len(c) for c in top], flush=True)

    final = []
    for c in clusters:
        final.extend(split_large(c, proj, a.max_size))
    kept = [c for c in final if len(c) >= a.min_size]
    dropped = sum(1 for c in final if len(c) < a.min_size)
    sizes = Counter()
    for c in kept:
        b = 1 if len(c) < 3 else 3 if len(c) < 10 else 10 if len(c) < 30 else 30 if len(c) < 100 else 100
        sizes[b] += 1
    print(f'  после деления: кластеров={len(final)}, отброшено (<{a.min_size})={dropped}, строим={len(kept)}', flush=True)
    print('  размеры (от N карточек → сколько кластеров):', dict(sorted(sizes.items())), flush=True)
    print('  крупнейшие итоговые:', sorted((len(c) for c in kept), reverse=True)[:10], flush=True)
    calls = sum(len(c) - 1 + 1 for c in kept)  # попарные слияния + описание
    print(f'  оценка вызовов модели ≈ {calls}, при ~0.05¢/вызов (flash-lite, короткие промпты) ≈ ${calls*0.0005:.2f}', flush=True)

    if a.dump:
        # имена карточек для глазной проверки кластеров
        names = {}
        rows, _, _ = await driver.execute_query('MATCH (n:Entity {group_id: $g}) RETURN n.uuid AS u, n.name AS n', g=a.group)
        for r in rows:
            names[r['u']] = r['n']
        json.dump([[names.get(u, u) for u in c] for c in sorted(kept, key=len, reverse=True)],
                  open(a.dump, 'w'), ensure_ascii=False, indent=0)
        print(f'  состав кластеров → {a.dump}', flush=True)

    if not a.apply:
        return

    api_key = os.environ['OPENAI_API_KEY']; base = os.environ.get('OPENAI_API_URL', 'https://openrouter.ai/api/v1')
    model = os.environ.get('MODEL_NAME', 'google/gemini-2.5-flash-lite')
    emb_model = os.environ.get('EMBEDDER_MODEL', 'BAAI/bge-m3'); emb_dim = int(os.environ.get('EMBEDDER_DIMENSIONS', '1024'))
    emb_base = os.environ.get('EMBEDDER_API_URL', base)
    llm, embedder, _ = make_clients(api_key, base, model, emb_model, emb_dim, emb_base)

    print(f'[{a.group}] удаляю старые сообщества…', flush=True)
    await co.remove_communities(driver, group_ids=[a.group])

    sem = asyncio.Semaphore(a.concurrency)
    done = 0; errors = 0; t1 = time.time()

    async def one(cluster):
        nonlocal done, errors
        async with sem:
            try:
                nodes = await EntityNode.get_by_uuids(driver, cluster)
                nodes = [n for n in nodes if n.summary]
                if len(nodes) < a.min_size:
                    return None
                node, edges = await co.build_community(llm, nodes)
                node.name = clean_name(node.name)
                await node.generate_name_embedding(embedder)
                await node.save(driver)
                await semaphore_gather(*[e.save(driver) for e in edges], max_coroutines=16)
                done += 1
                if done % 10 == 0:
                    print(f'  готово {done}/{len(kept)} ({time.time()-t1:.0f} с) последнее: {node.name[:80]!r} ({len(edges)} карточек)', flush=True)
                return node
            except Exception as e:
                errors += 1
                print(f'  !! кластер {len(cluster)} карточек: {e!r}', flush=True)
                return None

    results = await asyncio.gather(*[one(c) for c in kept])
    built = [r for r in results if r]
    print(f'[{a.group}] ГОТОВО: сообществ={len(built)} ошибок={errors} за {time.time()-t1:.0f} с', flush=True)
    for n in sorted(built, key=lambda n: n.name)[:200]:
        print('   •', n.name[:120], flush=True)
    await driver.close()


if __name__ == '__main__':
    asyncio.run(main())
