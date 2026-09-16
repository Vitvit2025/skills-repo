#!/usr/bin/env python3
"""Метрики качества графа после докачки (16.09.2026) — то, чего не хватало два дня: не «поиск выглядит нормально», а числа.

  .venv/bin/python metrics.py --since 2026-09-16T03:30:00Z [--graph main] [--json]

Считает по фактам/карточкам, созданным или погашенным ПОСЛЕ --since:
  fast_invalid   — факты, погашенные в этом прогоне в окне < 1 дня от valid_at (норма < 5 % от новых фактов; это «уточнение = опровержение»)
  generic_nodes  — новые карточки с нарицательным именем без собственного имени (норма: 0–3)
  new_facts / new_nodes / invalidated — объёмы
  top_degree     — 10 самых связанных карточек (хабы; резкий рост у нового узла = мусор-магнит)
  cost           — токены и $ по моделям из state/usage.jsonl с --since (цены OpenRouter в PRICES)
Код выхода 2 — метрика вышла за норму (крон шлёт алерт).
Секреты в запросы не подставляем (telemetry FalkorDB хранит тексты запросов в RDB)."""
import argparse, collections, datetime, json, os, re, sys
import redis

HERE = os.path.dirname(os.path.abspath(__file__))
PRICES = {  # $/M токенов (in, out), OpenRouter 16.09.2026
    'google/gemini-2.5-flash-lite': (0.10, 0.40), 'anthropic/claude-haiku-4.5': (1.0, 5.0), 'anthropic/claude-sonnet-5': (2.0, 10.0),
    'anthropic/claude-opus-5': (5.0, 25.0), 'google/gemini-2.5-flash': (0.30, 2.50), 'BAAI/bge-m3': (0.01, 0.0), 'baai/bge-m3': (0.01, 0.0)}
GENERIC = re.compile(r'^[а-яёa-z][а-яё\s-]{2,30}$')  # только строчные кириллица/латиница, без цифр/@/точек
GENERIC_OK = {'владелец', 'ассистент'}  # канонические имена, которые выглядят нарицательными


def p(x):
    try: return datetime.datetime.fromisoformat(str(x).replace('Z', '+00:00'))
    except Exception: return None


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--since', required=True); ap.add_argument('--graph', default='main'); ap.add_argument('--json', action='store_true')
    ap.add_argument('--fast-max', type=float, default=5.0, help='порог доли быстрых погашений, %'); ap.add_argument('--generic-max', type=int, default=3)
    a = ap.parse_args(); since = p(a.since); since_ts = since.timestamp()
    r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True)
    q = lambda c: r.execute_command('GRAPH.RO_QUERY', a.graph, c)[1]
    m = {}
    edges = q("MATCH (s)-[e:RELATES_TO]->(t) RETURN e.created_at, e.valid_at, e.invalid_at, e.expired_at, e.name, s.uuid, t.uuid")
    # погаситель = факт с valid_at == invalid_at ремонтируемого и общим узлом (так ставит Graphiti при противоречии);
    # без погасителя invalid_at пришёл из ИЗВЛЕЧЕНИЯ (модель сочла «попытка не сработала» концом факта) — другая метрика
    by_valid = collections.defaultdict(list)
    for created, va, ia, ea, name, su, tu in edges:
        if va: by_valid[str(va)].append((su, tu))
    new_f = inv = fast = fast_x = 0; fast_types = collections.Counter(); new_types = collections.Counter()
    for created, va, ia, ea, name, su, tu in edges:
        c, e = p(created), p(ea)
        if c and c >= since: new_f += 1; new_types[name] += 1
        if e and e >= since:
            inv += 1; v, i = p(va), p(ia)
            if v and i and (i - v).total_seconds() < 86400:
                if any(x in (su, tu) or y in (su, tu) for x, y in by_valid.get(str(ia), [])): fast += 1; fast_types[name] += 1
                else: fast_x += 1
    m['new_facts'] = new_f; m['invalidated_in_run'] = inv; m['fast_invalid'] = fast; m['fast_invalid_extracted'] = fast_x
    m['fast_invalid_pct'] = round(100 * fast / new_f, 1) if new_f else 0.0
    m['fast_invalid_types'] = dict(fast_types.most_common(5)); m['new_fact_types'] = dict(new_types.most_common(8))
    nodes = q("MATCH (n:Entity) RETURN n.name, n.created_at, labels(n)")
    new_n = 0; generic = []
    for name, created, labels in nodes:
        c = p(created)
        if c and c >= since:
            new_n += 1
            nm = (name or '').strip()
            if GENERIC.match(nm) and nm.lower() not in GENERIC_OK and len(nm.split()) <= 2: generic.append(nm)
    m['new_nodes'] = new_n; m['generic_nodes'] = len(generic); m['generic_examples'] = generic[:10]
    m['top_degree'] = [(n, d) for n, d in q("MATCH (n:Entity)-[e:RELATES_TO]-() RETURN n.name, count(e) AS d ORDER BY d DESC LIMIT 10")]
    # стоимость по моделям
    cost = collections.defaultdict(lambda: [0, 0, 0.0]); tags = collections.Counter()
    up = os.path.join(HERE, 'state', 'usage.jsonl')
    if os.path.exists(up):
        for line in open(up):
            try: u = json.loads(line)
            except Exception: continue
            if u.get('ts', 0) < since_ts: continue
            pr = PRICES.get((u.get('model') or '').lower(), PRICES.get(u.get('model') or '', (0, 0)))
            c = (u.get('in', 0) * pr[0] + u.get('out', 0) * pr[1]) / 1e6
            row = cost[u.get('model')]; row[0] += u.get('in', 0); row[1] += u.get('out', 0); row[2] += c; tags[u.get('tag', '')] += c
    m['cost_by_model'] = {k: {'in': v[0], 'out': v[1], 'usd': round(v[2], 3)} for k, v in cost.items()}
    m['cost_usd'] = round(sum(v[2] for v in cost.values()), 3); m['cost_by_tag'] = {k: round(v, 3) for k, v in tags.items()}
    alerts = []
    if new_f and m['fast_invalid_pct'] > a.fast_max: alerts.append(f'быстрых погашений {m["fast_invalid_pct"]}% > {a.fast_max}%')
    if m['generic_nodes'] > a.generic_max: alerts.append(f'нарицательных карточек {m["generic_nodes"]} > {a.generic_max}: {generic[:5]}')
    m['alerts'] = alerts
    if a.json: print(json.dumps(m, ensure_ascii=False))
    else:
        print(f"метрики с {a.since}: новых фактов {new_f}, погашено {inv} (быстрых <1д дедупом: {fast} = {m['fast_invalid_pct']}%, однодневных из извлечения: {fast_x}), "
              f"новых карточек {new_n}, нарицательных {len(generic)} {generic[:5]}; ${m['cost_usd']} {m['cost_by_tag']}; "
              f"хабы: {m['top_degree'][:3]}" + (f"; ⚠️ {'; '.join(alerts)}" if alerts else ''))
    sys.exit(2 if alerts else 0)


if __name__ == '__main__':
    main()
