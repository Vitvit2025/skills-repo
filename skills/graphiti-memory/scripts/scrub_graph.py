#!/usr/bin/env python3
"""Аудит и чистка секретов/телефонов в графах Graphiti (FalkorDB) тем же secret_filter, что и для транскриптов.
Проверяет Entity.name/summary, RELATES_TO.fact, Episodic.content. Запуск на хосте (словарь секретов — из файлов хоста):
  .venv/bin/python scrub_graph.py --graphs mem_prod,mem_dev,... [--apply]
Без --apply — только отчёт (по правилам и графам, контексты замаскированы)."""
import argparse, collections, re, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import redis
from secret_filter import SecretFilter

FIELDS = [('Entity', 'n', 'summary', 'MATCH (n:Entity) WHERE n.summary IS NOT NULL RETURN n.uuid, n.summary'),
          ('Entity', 'n', 'name', 'MATCH (n:Entity) RETURN n.uuid, n.name'),
          ('Episodic', 'n', 'content', 'MATCH (n:Episodic) WHERE n.content IS NOT NULL RETURN n.uuid, n.content'),
          ('RELATES_TO', 'r', 'fact', 'MATCH ()-[r:RELATES_TO]->() WHERE r.fact IS NOT NULL RETURN r.uuid, r.fact'),
          # сообщества: сводки собираются моделью из карточек — секрет из карточки уезжает и сюда (найдено 16.09.2026)
          ('Community', 'n', 'summary', 'MATCH (n:Community) WHERE n.summary IS NOT NULL RETURN n.uuid, n.summary'),
          ('Community', 'n', 'name', 'MATCH (n:Community) RETURN n.uuid, n.name')]


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--graphs', required=True); ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True)
    sf = SecretFilter(); print(f'словарь: {len(sf.known)} значений', file=sys.stderr)
    total = collections.Counter(); samples = []
    for g in a.graphs.split(','):
        per = collections.Counter(); changed = 0
        for label, var, field, cypher in FIELDS:
            res = r.execute_command('GRAPH.RO_QUERY', g, cypher)
            rows = res[1]
            for uuid, text in rows:
                if not text: continue
                clean, st = sf.redact(text, exclude=('kv_ru',))
                if clean == text: continue
                per.update(st); changed += 1
                for m in re.finditer(r'\[REDACTED:(\w+)\]', clean):
                    if len(samples) < 40: samples.append((g, label, field, clean[max(0, m.start()-45):m.end()+20].replace('\n', ' ')))
                if a.apply:
                    # 🔴 у факта/имени есть эмбеддинг: без пересчёта поиск по вырезанному значению всё ещё находил бы этот факт
                    emb = ''
                    if field in ('fact', 'name'):
                        vec = _embed(clean)
                        if vec: emb = f', {var}.{field}_embedding = vecf32([' + ','.join(f'{x:.7g}' for x in vec) + '])'
                    if var == 'n':
                        r.execute_command('GRAPH.QUERY', g, f'MATCH (n:{label} {{uuid: "{uuid}"}}) SET n.{field} = ' + _lit(clean) + emb)
                    else:
                        r.execute_command('GRAPH.QUERY', g, f'MATCH ()-[r:RELATES_TO {{uuid: "{uuid}"}}]->() SET r.{field} = ' + _lit(clean) + emb)
        total.update(per)
        print(f'[{g}] полей с секретами/телефонами: {changed}; по правилам: {dict(per)}{" — ИСПРАВЛЕНО" if a.apply else ""}')
    print('итого по правилам:', dict(total))
    print('--- примеры (после замены):')
    for g, label, field, ctx in samples[:40]: print(f'  {g}/{label}.{field}: …{ctx}…')


def _embed(text: str):
    """Эмбеддинг локальным TEI bge-m3 (тот же, что у MCP: 127.0.0.1:18081, 1024 dims). Ошибка → None (поле остаётся старым, пишем в stderr)."""
    import json as _j, urllib.request
    url = os.environ.get('EMBEDDER_API_URL', 'http://127.0.0.1:18081/v1') + '/embeddings'
    try:
        req = urllib.request.Request(url, data=_j.dumps({'input': text[:8000], 'model': 'BAAI/bge-m3'}).encode(), headers={'Content-Type': 'application/json'})
        return _j.load(urllib.request.urlopen(req, timeout=30))['data'][0]['embedding']
    except Exception as e:
        print(f'  !! эмбеддинг не пересчитан: {e!r}', file=sys.stderr); return None


def _lit(s: str) -> str:
    """Строковый литерал Cypher (FalkorDB): экранируем \\ и \", переносы — как \\n."""
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '') + '"'


if __name__ == '__main__':
    main()
