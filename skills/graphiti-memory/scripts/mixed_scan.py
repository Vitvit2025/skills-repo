#!/usr/bin/env python3
"""Поиск «сшитых» карточек (16.09.2026): сводка, в которой смешаны РАЗНЫЕ объекты с похожим именем (Баранов-автор +
Баранов-подрядчик; «банк» = Альфа-банк + банк заданий). Дешёвая модель классифицирует сводки батчами по 15.

  .venv/bin/python mixed_scan.py --graph main [--min-chars 250] [--model google/gemini-2.5-flash-lite] [--limit N]
Результат: state/mixed_candidates.json + печать кандидатов (имя, тип, почему). Решение о расклейке — руками (split_node.py)."""
import argparse, json, os, sys, time, urllib.request
import redis

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPT = """Ниже карточки из графа знаний (имя, тип, сводка). Для каждой ответь: описывает ли сводка ОДИН реальный объект
(человека, сервер, сервис, проект…), или в ней СМЕШАНЫ разные объекты, случайно получившие одно имя (однофамильцы,
нарицательное слово, разные вещи с похожим названием)? Признаки смеси: несовместимые роли/контексты у одного имени
(автор учебников и одновременно подрядчик стройки; банк как организация и «банк заданий»; сервер и одноимённый скрипт).
Разные факты об одном объекте — НЕ смесь. Ответ: только JSON-массив {{"i": <номер>, "mixed": true|false, "why": "<до 12 слов>"}} для всех.

{items}"""


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--graph', default='main'); ap.add_argument('--min-chars', type=int, default=250)
    ap.add_argument('--model', default='google/gemini-2.5-flash-lite'); ap.add_argument('--limit', type=int, default=0); a = ap.parse_args()
    r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True)
    rows = r.execute_command('GRAPH.RO_QUERY', a.graph, f"MATCH (n:Entity) WHERE size(n.summary) >= {a.min_chars} RETURN n.uuid, n.name, labels(n), n.summary")[1]
    if a.limit: rows = rows[:a.limit]
    api_key = next((l.split('=', 1)[1].strip() for l in open(os.path.join(HERE, '.env')) if l.startswith('OPENAI_API_KEY=')), None)
    print(f'карточек со сводкой ≥{a.min_chars} симв: {len(rows)}', flush=True)
    found = []; tok = [0, 0]
    for bi in range(0, len(rows), 15):
        batch = rows[bi:bi + 15]
        items = '\n\n'.join(f'{i+1}. {n} [{", ".join(l for l in lb if l != "Entity")}]: {s[:900]}' for i, (u, n, lb, s) in enumerate(batch))
        req = urllib.request.Request('https://openrouter.ai/api/v1/chat/completions', data=json.dumps({'model': a.model, 'messages': [{'role': 'user', 'content': PROMPT.format(items=items)}], 'temperature': 0, 'max_tokens': 1500}).encode(),
                                     headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'})
        try:
            res = json.load(urllib.request.urlopen(req, timeout=120)); u = res.get('usage') or {}; tok[0] += u.get('prompt_tokens', 0); tok[1] += u.get('completion_tokens', 0)
            txt = res['choices'][0]['message']['content']; txt = txt[txt.find('['):txt.rfind(']') + 1]
            for o in json.loads(txt):
                if o.get('mixed'):
                    uu, n, lb, s = batch[int(o['i']) - 1]; found.append({'uuid': uu, 'name': n, 'labels': lb, 'why': o.get('why', ''), 'summary': s[:400]})
        except Exception as e: print(f'  !! batch {bi//15+1}: {e!r}', file=sys.stderr)
        if (bi // 15) % 40 == 0: print(f'  {min(bi+15, len(rows))}/{len(rows)}, кандидатов {len(found)}', flush=True)
    with open(os.path.join(HERE, 'state', 'usage.jsonl'), 'a') as f: f.write(json.dumps({'ts': time.time(), 'tag': 'mixed_scan', 'model': a.model, 'in': tok[0], 'out': tok[1]}) + '\n')
    json.dump(found, open(os.path.join(HERE, 'state', 'mixed_candidates.json'), 'w'), ensure_ascii=False, indent=1)
    print(f'кандидатов на расклейку: {len(found)}; токены {tok}')
    for c in found: print(f'  • {c["name"]} [{", ".join(l for l in c["labels"] if l != "Entity")}] — {c["why"]}')


if __name__ == '__main__':
    main()
