#!/usr/bin/env python3
"""Аудит и чистка секретов/телефонов в графах Graphiti (FalkorDB) тем же secret_filter, что и для транскриптов.
Проверяет Entity.name/summary, RELATES_TO.fact, Episodic.content. Запуск НА ХОСТЕ (словарь секретов — из файлов хоста;
нужен пакет redis: pip install redis):
  python3 scrub_graph.py [--graphs main] [--apply]
Без --apply — только отчёт (по правилам и графам, контексты замаскированы). После любой докачки ожидается 0.
Эмбеддинги отредактированных полей не пересчитываются (несущественно)."""
import argparse, collections, re, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import redis
from secret_filter import SecretFilter
from gm_config import cfg

FIELDS = [('Entity', 'n', 'summary', 'MATCH (n:Entity) WHERE n.summary IS NOT NULL RETURN n.uuid, n.summary'),
          ('Entity', 'n', 'name', 'MATCH (n:Entity) RETURN n.uuid, n.name'),
          ('Episodic', 'n', 'content', 'MATCH (n:Episodic) WHERE n.content IS NOT NULL RETURN n.uuid, n.content'),
          ('RELATES_TO', 'r', 'fact', 'MATCH ()-[r:RELATES_TO]->() WHERE r.fact IS NOT NULL RETURN r.uuid, r.fact')]


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--graphs', default=None, help='через запятую; по умолчанию graph из конфига')
    ap.add_argument('--apply', action='store_true'); ap.add_argument('--config', default=None)
    ap.add_argument('--host', default='127.0.0.1'); ap.add_argument('--port', type=int, default=None)
    a = ap.parse_args()
    c = cfg(a.config); graphs = (a.graphs or c.graph).split(',')
    port = a.port or (6379 if c.inside else int(c.get('ports.falkordb', 6379)))
    exclude = tuple(c.get('secrets.exclude_rules_prose', ['kv_ru']) or [])
    r = redis.Redis(host=a.host, port=port, decode_responses=True)
    sf = SecretFilter(config=a.config); print(f'словарь: {len(sf.known)} значений', file=sys.stderr)
    total = collections.Counter(); samples = []
    for g in graphs:
        per = collections.Counter(); changed = 0
        for label, var, field, cypher in FIELDS:
            res = r.execute_command('GRAPH.RO_QUERY', g, cypher)
            for uuid, text in res[1]:
                if not text: continue
                clean, st = sf.redact(text, exclude=exclude)
                if clean == text: continue
                per.update(st); changed += 1
                for m in re.finditer(r'\[REDACTED:(\w+)\]', clean):
                    if len(samples) < 40: samples.append((g, label, field, clean[max(0, m.start()-45):m.end()+20].replace('\n', ' ')))
                if a.apply:
                    if var == 'n':
                        r.execute_command('GRAPH.QUERY', g, f'MATCH (n:{label} {{uuid: "{uuid}"}}) SET n.{field} = ' + _lit(clean))
                    else:
                        r.execute_command('GRAPH.QUERY', g, f'MATCH ()-[r:RELATES_TO {{uuid: "{uuid}"}}]->() SET r.{field} = ' + _lit(clean))
        total.update(per)
        print(f'[{g}] полей с секретами/телефонами: {changed}; по правилам: {dict(per)}{" — ИСПРАВЛЕНО" if a.apply else ""}')
    print('итого по правилам:', dict(total))
    print('--- примеры (после замены):')
    for g, label, field, ctx in samples[:40]: print(f'  {g}/{label}.{field}: …{ctx}…')


def _lit(s: str) -> str:
    """Строковый литерал Cypher (FalkorDB): экранируем \\ и \", переносы — как \\n."""
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '') + '"'


if __name__ == '__main__':
    main()
