#!/usr/bin/env python3
"""Расклейка ошибочно слитой карточки по провенансу (16.09.2026): у каждого факта есть список эпизодов (e.episodes),
у карточки — рёбра MENTIONS от эпизодов. Рёбра, чьи эпизоды подходят под --episodes-match (regex по тексту эпизода),
перевешиваем на НОВУЮ карточку с именем --new-name; MENTIONS этих эпизодов — тоже. Сводка новой карточки = --summary,
эмбеддинг имени — локальный TEI. Старая карточка остаётся с остальными рёбрами (её сводку стоит переписать руками/моделью).

  .venv/bin/python split_node.py --graph main --node "Баранов Владимир Николаевич" --episodes-match "ЕГЭ|ege-obsh|пособи|вариант" \
      --new-name "Пётр Баранов (автор пособий ЕГЭ)" --new-label Human --summary "…" [--apply]
"""
import argparse, json, os, re, sys, uuid as uuidlib, urllib.request
import redis

HERE = os.path.dirname(os.path.abspath(__file__))


def lit(s): return '"' + s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'


def embed(text):
    req = urllib.request.Request(os.environ.get('EMBEDDER_API_URL', 'http://127.0.0.1:18081/v1') + '/embeddings',
                                 data=json.dumps({'input': text, 'model': 'BAAI/bge-m3'}).encode(), headers={'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=30))['data'][0]['embedding']


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--graph', default='main'); ap.add_argument('--node', required=True, help='имя или uuid карточки')
    ap.add_argument('--episodes-match', required=True); ap.add_argument('--new-name', required=True); ap.add_argument('--new-label', default='Human')
    ap.add_argument('--summary', default=''); ap.add_argument('--apply', action='store_true')
    ap.add_argument('--target', default='', help='имя/uuid СУЩЕСТВУЮЩЕЙ карточки, на которую перевесить (вместо создания новой)'); a = ap.parse_args()
    r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True)
    def q(c):  # у запросов без RETURN FalkorDB отдаёт только статистику (1 элемент)
        res = r.execute_command('GRAPH.QUERY', a.graph, c)
        return res[1] if len(res) > 1 else []
    rows = q(f'MATCH (n:Entity) WHERE n.uuid = {lit(a.node)} OR n.name = {lit(a.node)} RETURN n.uuid, n.name, n.group_id, labels(n), left(n.summary, 300)')
    if len(rows) != 1: print('карточка не найдена или не одна:', rows); sys.exit(1)
    nu, name, gid, labels, summ = rows[0]; print(f'карточка: {name} {labels}\n  сводка: {summ}')
    rx = re.compile(a.episodes_match, re.I)
    eps = q(f'MATCH (ep:Episodic)-[:MENTIONS]->(n:Entity {{uuid: {lit(nu)}}}) RETURN ep.uuid, ep.name, left(ep.content, 4000)')
    move_eps = {u for u, n, c in eps if rx.search((n or '') + ' ' + (c or ''))}
    print(f'эпизодов у карточки: {len(eps)}, под маску: {len(move_eps)}')
    edges = q(f'MATCH (s)-[e:RELATES_TO]-(t) WHERE s.uuid = {lit(nu)} RETURN e.uuid, e.episodes, e.fact, startNode(e).uuid = {lit(nu)}')
    move_edges = []
    for eu, ep_list, fact, is_src in edges:
        ep_ids = [x.strip() for x in str(ep_list).strip('[]').split(',') if x.strip()]
        if ep_ids and all(x in move_eps for x in ep_ids): move_edges.append((eu, fact, is_src))
        elif ep_ids and any(x in move_eps for x in ep_ids): print(f'  ? смешанный факт (эпизоды из обеих тем), оставляю: {fact[:90]}')
    print(f'рёбер у карточки: {len(edges)}, перевесить: {len(move_edges)}')
    for eu, fact, is_src in move_edges[:15]: print('   →', fact[:110])
    if not a.apply: print('(план; --apply чтобы применить)'); return
    if a.target:
        t = q(f'MATCH (n:Entity) WHERE n.uuid = {lit(a.target)} OR n.name = {lit(a.target)} RETURN n.uuid, n.name')
        if len(t) != 1: print('целевая карточка не найдена или не одна:', t); sys.exit(1)
        new_uuid = t[0][0]; print(f'перевешиваю на существующую карточку {t[0][1]} ({new_uuid})')
        if a.new_name != t[0][1]: q(f'MATCH (n:Entity {{uuid: {lit(new_uuid)}}}) SET n.name = {lit(a.new_name)}, n.name_embedding = vecf32([{",".join(f"{x:.7g}" for x in embed(a.new_name))}])')
        if a.summary: q(f'MATCH (n:Entity {{uuid: {lit(new_uuid)}}}) SET n.summary = {lit(a.summary)}')
    else:
        new_uuid = str(uuidlib.uuid4()); vec = embed(a.new_name)
        lbl = ':'.join(['Entity', a.new_label])
        q(f'CREATE (n:{lbl} {{uuid: {lit(new_uuid)}, name: {lit(a.new_name)}, group_id: {lit(gid)}, summary: {lit(a.summary)}, '
          f'created_at: {lit(__import__("datetime").datetime.utcnow().isoformat(timespec="seconds") + "+00:00")}, labels: ["Entity","{a.new_label}"]}}) '
          f'SET n.name_embedding = vecf32([{",".join(f"{x:.7g}" for x in vec)}])')
    moved = 0
    for eu, fact, is_src in move_edges:
        # перевешиваем ребро: копия свойств на новое ребро, старое удаляем (uuid/эмбеддинг сохраняются)
        if is_src:
            q(f'MATCH (o:Entity {{uuid: {lit(nu)}}})-[e:RELATES_TO {{uuid: {lit(eu)}}}]->(t), (n:Entity {{uuid: {lit(new_uuid)}}}) CREATE (n)-[e2:RELATES_TO]->(t) SET e2 = properties(e) DELETE e')
        else:
            q(f'MATCH (s)-[e:RELATES_TO {{uuid: {lit(eu)}}}]->(o:Entity {{uuid: {lit(nu)}}}), (n:Entity {{uuid: {lit(new_uuid)}}}) CREATE (s)-[e2:RELATES_TO]->(n) SET e2 = properties(e) DELETE e')
        moved += 1
    for eu in move_eps:
        q(f'MATCH (ep:Episodic {{uuid: {lit(eu)}}})-[m:MENTIONS]->(o:Entity {{uuid: {lit(nu)}}}), (n:Entity {{uuid: {lit(new_uuid)}}}) CREATE (ep)-[m2:MENTIONS]->(n) SET m2 = properties(m) DELETE m')
    print(f'создана {a.new_name} ({new_uuid}); перевешено рёбер {moved}, MENTIONS {len(move_eps)}')


if __name__ == '__main__':
    main()
