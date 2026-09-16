#!/usr/bin/env python3
"""Ремонт ложных погашений фактов (16.09.2026): штатный промпт дедупа считал уточнение опровержением →
17.7k фактов с invalid_at, 6.3k из них «прожили» < 1 дня (пересказ того же в соседнем куске).

  .venv/bin/python repair_invalidations.py --graph main [--apply] [--llm-days 7] [--model anthropic/claude-sonnet-5]

Погашение считаем «ремонтопригодным», только если найден ФАКТ-ПОГАСИТЕЛЬ: факт с valid_at == invalid_at ремонтируемого
(так ставит Graphiti: edge.invalid_at = resolved_edge.valid_at), с общим узлом и созданный не раньше. Если погасителя нет —
invalid_at пришёл из текста («до 26.07», «закрыто 14.09») — это история, не трогаем.
Правило 1 (без модели): окно invalid_at − valid_at < 24 ч и есть погаситель → снять invalid_at/expired_at.
Правило 2 (модель): окно 1..--llm-days дней и есть погаситель → модель батчами по 20 пар: «могут ли оба быть верны
одновременно?» → contradiction=false → снять. Окно больше — не трогаем (реальная история: переезд, закрытие).
Откат: state/repair_<ts>.json хранит старые invalid_at/expired_at по uuid. Секреты в запросы не подставляются.
"""
import argparse, collections, datetime, json, os, sys, time, urllib.request
import redis

HERE = os.path.dirname(os.path.abspath(__file__))
USAGE = os.path.join(HERE, 'state', 'usage.jsonl')
PROMPT = """Ниже пары фактов из графа знаний об инфраструктуре и проектах (русский/английский). В каждой паре СТАРЫЙ факт был
помечен как опровергнутый НОВЫМ. Для каждой пары ответь, действительно ли они НЕ МОГУТ быть верны одновременно.
Противоречие (true) — только если тот же субъект и та же связь получили взаимоисключающее значение (другой порт, другой
сервер, другая модель, включено→выключено, работает→удалён) или новый факт явно завершает старый («больше не», «удалён»,
«заменён на», «переехал», «закрыто»). Уточнение, деталь, перевод, другая формулировка, другой аспект, план и его
результат, факты о разных машинах — НЕ противоречие (false). При сомнении — false.
Ответ: только JSON-массив объектов {{"i": <номер>, "contradiction": true|false}} для ВСЕХ пар.

{pairs}"""


def p(x):
    try: return datetime.datetime.fromisoformat(str(x).replace('Z', '+00:00'))
    except Exception: return None


def llm(api_key, model, prompt):
    req = urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',
                                 data=json.dumps({'model': model, 'messages': [{'role': 'user', 'content': prompt}], 'temperature': 0, 'max_tokens': 2000}).encode(),
                                 headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'})
    for attempt in range(3):
        try:
            r = json.load(urllib.request.urlopen(req, timeout=180)); u = r.get('usage') or {}
            with open(USAGE, 'a') as f: f.write(json.dumps({'ts': time.time(), 'tag': 'repair', 'model': model, 'in': u.get('prompt_tokens', 0), 'out': u.get('completion_tokens', 0)}) + '\n')
            txt = r['choices'][0]['message']['content'].strip()
            txt = txt[txt.find('['):txt.rfind(']') + 1]
            return {int(o['i']): bool(o['contradiction']) for o in json.loads(txt)}
        except Exception as e:
            if attempt == 2: print(f'  !! llm: {e!r}', file=sys.stderr); return {}
            time.sleep(5)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--graph', default='main'); ap.add_argument('--apply', action='store_true')
    ap.add_argument('--llm-days', type=float, default=7); ap.add_argument('--model', default='anthropic/claude-sonnet-5'); ap.add_argument('--limit-llm', type=int, default=0)
    ap.add_argument('--same-day', action='store_true', help='режим извлечения: снять invalid_at у фактов с окном < 1 дня БЕЗ требования погасителя '
                    '(модель извлечения ставит «попытка не сработала» = закончилось в тот же день; для графа знаний это артефакт)')
    ap.add_argument('--since', default='', help='с --same-day: только факты, СОЗДАННЫЕ не раньше этого времени')
    ap.add_argument('--expired-since', default='', help='с --same-day: факты, ПОГАШЕННЫЕ (expired_at) не раньше этого времени — в т.ч. старые, '
                    'которые прогон погасил пересказом того же (окно valid_at→invalid_at < 1 дня у давнего факта = обе даты из одного дня = пересказ)')
    a = ap.parse_args()
    r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True)
    q = lambda c: r.execute_command('GRAPH.RO_QUERY', a.graph, c)[1]
    api_key = next((l.split('=', 1)[1].strip() for l in open(os.path.join(HERE, '.env')) if l.startswith('OPENAI_API_KEY=')), None)
    rows = q("MATCH (s)-[e:RELATES_TO]->(t) RETURN e.uuid, s.uuid, t.uuid, e.valid_at, e.invalid_at, e.expired_at, e.created_at, e.fact")
    by_valid = collections.defaultdict(list)   # valid_at → факты (кандидаты в погасители)
    edges = {}
    for u, s, t, va, ia, ea, ca, fact in rows:
        edges[u] = (s, t, va, ia, ea, ca, fact or '')
        if va: by_valid[str(va)].append(u)
    stats = collections.Counter(); rule1 = []; llm_pairs = []
    since = p(a.since) if a.since else None
    for u, (s, t, va, ia, ea, ca, fact) in edges.items():
        if not ia: continue
        v, i = p(va), p(ia)
        if not v or not i: stats['нет дат'] += 1; continue
        win = (i - v).total_seconds() / 86400
        if a.same_day:
            c, ex = p(ca), p(ea)
            if since and (not c or c < since): continue
            if a.expired_since and (not ex or ex < p(a.expired_since)): continue
            if win < 1: rule1.append((u, u)); stats['однодневные'] += 1
            continue
        cands = [c for c in by_valid.get(str(ia), []) if c != u and (edges[c][0] in (s, t) or edges[c][1] in (s, t)) and str(edges[c][5]) >= str(ca)]
        if not cands: stats['без погасителя (история)'] += 1; continue
        if win < 1: rule1.append((u, cands[0])); stats['правило 1 (<1д)'] += 1
        elif win <= a.llm_days: llm_pairs.append((u, cands[0])); stats[f'на модель (1..{a.llm_days:g}д)'] += 1
        else: stats['окно больше — не трогаем'] += 1
    print('разбор погашений:', dict(stats))
    restore = list(rule1)
    if a.limit_llm: llm_pairs = llm_pairs[:a.limit_llm]
    verdicts = {}
    for bi in range(0, len(llm_pairs), 20):
        batch = llm_pairs[bi:bi + 20]
        pairs = '\n\n'.join(f'{i+1}. СТАРЫЙ: {edges[u][6][:300]}\n   НОВЫЙ: {edges[c][6][:300]}' for i, (u, c) in enumerate(batch))
        res = llm(api_key, a.model, PROMPT.format(pairs=pairs))
        for i, (u, c) in enumerate(batch):
            verdicts[u] = res.get(i + 1, True)   # нет ответа → считаем противоречием (не трогаем)
        done = min(bi + 20, len(llm_pairs))
        if done % 200 < 20 or done == len(llm_pairs): print(f'  модель: {done}/{len(llm_pairs)} пар, снять {sum(1 for v in verdicts.values() if not v)}', flush=True)
    restore += [(u, c) for u, c in llm_pairs if not verdicts.get(u, True)]
    print(f'к восстановлению: {len(restore)} (правило 1: {len(rule1)}, модель: {len(restore) - len(rule1)} из {len(llm_pairs)})')
    for u, c in restore[:8]:
        print(f'   {str(edges[u][2])[:16]}→{str(edges[u][3])[:16]}  СТАРЫЙ: {edges[u][6][:90]}' + ('' if c == u else f' | НОВЫЙ: {edges[c][6][:90]}'))
    if not a.apply: print('(план; --apply чтобы применить)'); return
    log_path = os.path.join(HERE, 'state', f'repair_{datetime.datetime.now(datetime.timezone.utc):%Y%m%d-%H%M}.json')
    json.dump({u: {'invalid_at': edges[u][3], 'expired_at': edges[u][4], 'by': c} for u, c in restore}, open(log_path, 'w'), ensure_ascii=False)
    n = 0
    for u, _ in restore:
        r.execute_command('GRAPH.QUERY', a.graph, f'MATCH ()-[e:RELATES_TO {{uuid: "{u}"}}]->() SET e.invalid_at = NULL, e.expired_at = NULL'); n += 1
    print(f'восстановлено {n} фактов; откат — {log_path}')


if __name__ == '__main__':
    main()
