#!/usr/bin/env python3
"""Пост-проход: склейка дублей сущностей в графе FalkorDB (Graphiti) по таблице псевдонимов `aliases` из graphiti-memory.yaml.
Запуск ВНУТРИ контейнера:  /app/mcp/.venv/bin/python /app/loaders/merge_aliases.py [--graph main] [--apply]
(на хосте — с falkordb-клиентом: --host 127.0.0.1 --port <ports.falkordb>). Без --apply — только план. Правила:
  1. aliases: канонический узел ← псевдонимы (без учёта регистра, обрезка пробелов/кавычек). Метки объединяются.
  2. Server-узел вида IP:port / IP/32 → голый IP (если есть, иначе переименовать); alias_rules.local_ips не трогаем.
  3. Одинаковое нормализованное имя + одинаковая метка → один узел (остаётся с самой длинной сводкой).
Механика: все RELATES_TO (в обе стороны), MENTIONS (из Episodic), HAS_MEMBER перевешиваются на канонический узел
с копией ВСЕХ свойств ребра (uuid, fact, fact_embedding, даты) → списки entity_edges в эпизодах остаются валидны.
Рёбра между дублем и каноническим (были бы петлёй) удаляются. Сводки склеиваются через « | » (≤3000 симв.).
Прогонять после КАЖДОЙ докачки; новые прозвища — дописывать в aliases. Перед первым боевым прогоном — GRAPH.COPY и проверка на копии.
"""
import argparse, os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from falkordb import FalkorDB
from gm_config import cfg

IP_PORT = re.compile(r'^(\d{1,3}(?:\.\d{1,3}){3})(?::\d+|/32)$')  # подсети /24 и т.п. — не хосты, не трогаем


def norm(s): return re.sub(r'\s+', ' ', (s or '').strip().strip('«»"\'` ').lower())


def q(g, cypher, **params):
    return g.query(cypher, params).result_set


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--graph', default=None); ap.add_argument('--apply', action='store_true')
    ap.add_argument('--host', default='localhost'); ap.add_argument('--port', type=int, default=None); ap.add_argument('--config', default=None)
    a = ap.parse_args()
    c = cfg(a.config); graph = a.graph or c.graph
    port = a.port or (6379 if c.inside else int(c.get('ports.falkordb', 6379)))
    aliases = {k: ((v or {}).get('labels') or [], [str(x) for x in ((v or {}).get('aliases') or [])]) for k, v in (c.get('aliases') or {}).items()}
    local_ips = set(c.get('alias_rules.local_ips', ['127.0.0.1', '172.17.0.1', 'localhost', '0.0.0.0']) or [])
    g = FalkorDB(host=a.host, port=port).select_graph(graph)
    nodes = q(g, 'MATCH (n:Entity) RETURN n.uuid, n.name, labels(n), size(coalesce(n.summary, ""))')
    by_norm = {}
    for uuid, name, labels, slen in nodes: by_norm.setdefault(norm(name), []).append((uuid, name, [l for l in labels if l != 'Entity'], slen))
    plan = []  # (keep_uuid, keep_name, dup_uuid, dup_name, add_labels, rule)
    registry = {}  # norm(каноническое имя) → uuid узла, который его получит

    def pick_keep(cands):  # самый длинный summary
        return max(cands, key=lambda c: c[3])

    # 1. псевдонимы
    for canon, (add_labels, als) in aliases.items():
        canon = str(canon)
        cands = list(by_norm.get(norm(canon), []))
        alias_nodes = [x for al in als for x in by_norm.get(norm(al), [])]
        if not alias_nodes and len(cands) <= 1: continue
        if cands: keep = pick_keep(cands); rename = None if keep[1] == canon else canon  # «ВЛАДЕЛЕЦ» → «Владелец»
        else: keep = pick_keep(alias_nodes); rename = canon
        for x in cands + alias_nodes:
            if x[0] != keep[0]: plan.append((keep[0], keep[1], x[0], x[1], add_labels, f'alias→{canon}'))
        if rename: plan.append((keep[0], keep[1], None, None, add_labels, f'rename→{canon}'))
        registry[norm(canon)] = keep[0]
    merged = {p[2] for p in plan if p[2]}
    # 2. IP:port → IP
    for key, cands in by_norm.items():
        for x in cands:
            if x[0] in merged: continue
            m = IP_PORT.match(x[1] or '')
            if not m or m.group(1) in local_ips: continue
            ip = m.group(1)
            base_uuid = registry.get(norm(ip))
            if not base_uuid:
                base = [b for b in by_norm.get(norm(ip), []) if b[0] not in merged]
                base_uuid = base[0][0] if base else None
            if base_uuid and base_uuid != x[0]: plan.append((base_uuid, ip, x[0], x[1], ['Server'], 'ip:port→ip'))
            else: plan.append((x[0], x[1], None, None, ['Server'], f'rename→{ip}')); registry[norm(ip)] = x[0]
            merged.add(x[0])
    # 3. одинаковое нормализованное имя + метка
    for key, cands in by_norm.items():
        cands = [x for x in cands if x[0] not in merged]
        if len(cands) < 2: continue
        by_label = {}
        for x in cands: by_label.setdefault(tuple(sorted(x[2])), []).append(x)
        for lab, cs in by_label.items():
            if len(cs) < 2: continue
            keep = pick_keep(cs)
            for x in cs:
                if x[0] != keep[0]: plan.append((keep[0], keep[1], x[0], x[1], [], 'same-name')); merged.add(x[0])

    n0 = q(g, 'MATCH (n:Entity) RETURN count(n)')[0][0]; e0 = q(g, 'MATCH ()-[r:RELATES_TO]->() RETURN count(r)')[0][0]
    print(f'[{graph}] узлов {n0}, фактов {e0}; план: {len([p for p in plan if p[2]])} склеек, {len([p for p in plan if not p[2]])} переименований')
    for keep_u, keep_n, dup_u, dup_n, labels, rule in plan:
        print(f'   {rule:22} {dup_n!r} → {keep_n!r}' if dup_u else f'   {rule:22} {keep_n!r}')
    if not a.apply: return
    t0 = time.time(); moved = 0
    for keep_u, keep_n, dup_u, dup_n, labels, rule in plan:
        if rule.startswith('rename→'):
            q(g, 'MATCH (k:Entity {uuid:$k}) SET k.name = $name', k=keep_u, name=rule.split('→', 1)[1])
        if dup_u:
            q(g, 'MATCH (d:Entity {uuid:$d})-[r:RELATES_TO]-(k:Entity {uuid:$k}) DELETE r', d=dup_u, k=keep_u)  # будущие петли
            moved += q(g, 'MATCH (d:Entity {uuid:$d})-[r:RELATES_TO]->(m) MATCH (k:Entity {uuid:$k}) CREATE (k)-[r2:RELATES_TO]->(m) SET r2 = properties(r) DELETE r RETURN count(r2)', d=dup_u, k=keep_u)[0][0]
            moved += q(g, 'MATCH (m)-[r:RELATES_TO]->(d:Entity {uuid:$d}) MATCH (k:Entity {uuid:$k}) CREATE (m)-[r2:RELATES_TO]->(k) SET r2 = properties(r) DELETE r RETURN count(r2)', d=dup_u, k=keep_u)[0][0]
            q(g, 'MATCH (e:Episodic)-[r:MENTIONS]->(d:Entity {uuid:$d}) MATCH (k:Entity {uuid:$k}) WHERE NOT (e)-[:MENTIONS]->(k) CREATE (e)-[r2:MENTIONS]->(k) SET r2 = properties(r)', d=dup_u, k=keep_u)
            q(g, 'MATCH (c:Community)-[r:HAS_MEMBER]->(d:Entity {uuid:$d}) MATCH (k:Entity {uuid:$k}) WHERE NOT (c)-[:HAS_MEMBER]->(k) CREATE (c)-[r2:HAS_MEMBER]->(k) SET r2 = properties(r)', d=dup_u, k=keep_u)
            q(g, 'MATCH (d:Entity {uuid:$d}), (k:Entity {uuid:$k}) SET k.summary = left(coalesce(k.summary, "") + CASE WHEN coalesce(d.summary, "") = "" THEN "" ELSE " | " + d.summary END, 3000)', d=dup_u, k=keep_u)
            q(g, 'MATCH (d:Entity {uuid:$d}) DETACH DELETE d', d=dup_u)
        for lab in labels:
            q(g, f'MATCH (k:Entity {{uuid:$k}}) SET k:{lab}', k=keep_u)
    n1 = q(g, 'MATCH (n:Entity) RETURN count(n)')[0][0]; e1 = q(g, 'MATCH ()-[r:RELATES_TO]->() RETURN count(r)')[0][0]
    dangling = q(g, 'MATCH (e:Episodic) WHERE size(e.entity_edges) > 0 UNWIND e.entity_edges AS u OPTIONAL MATCH ()-[r:RELATES_TO {uuid:u}]->() WITH u, r WHERE r IS NULL RETURN count(u)')[0][0]
    print(f'[{graph}] готово за {time.time()-t0:.0f} с: узлов {n0}→{n1}, фактов {e0}→{e1} (перевешено {moved}), ссылок эпизодов на пропавшие рёбра: {dangling}')


if __name__ == '__main__':
    main()
