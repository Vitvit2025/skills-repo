#!/usr/bin/env python3
"""Дайджест сессии Claude Code вместо сырых транскриптов (решение владельца 16.09.2026).

Зачем: сырые куски транскрипта (30–140 на сессию) тащат в граф петли «решили → переделали → решили» и стоят ~$50/мес.
Дайджест = 1–3 куска на сессию: решения и результаты, тупики с причиной, открытые вопросы, правила владельца.

Как: транскрипт → только реплики владельца/ассистента (sessions_extract.turns_of) → secret_filter → модель (OpenRouter,
anthropic/claude-sonnet-5) по жёсткой форме → secret_filter ещё раз → sessions_filtered/episodes_digest.jsonl
(формат как у bulk_load_sessions.py; имя digest_<sid>@<sha1 транскрипта>[:8], группа digest).
Состояние: state/digests.json {sid: {hash, ts, chars}} — сессия с тем же хешем не пересчитывается; выросшая сессия
даёт новый дайджест (старый остаётся в графе как история; дедуп склеит повторы).
Идущие сессии пропускаем: --min-idle-hours (по mtime файла). Стоимость пишется в state/usage.jsonl (tag=digest).

  python3 session_digest.py --src /root/.claude/projects/-root --out sessions_filtered [--min-idle-hours 2] [--files id1,id2] [--dry-run]
"""
import argparse, collections, datetime, glob, hashlib, json, os, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from secret_filter import SecretFilter
from sessions_extract import turns_of

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, 'state', 'digests.json')
USAGE = os.path.join(HERE, 'state', 'usage.jsonl')
DIGEST_DIR = os.path.join(HERE, 'state', 'digests')
PART_CHARS = 350_000       # ≈90k токенов: сессии длиннее режем на части, дайджесты частей сводим ещё одним вызовом
MIN_CHARS = 800            # короче — нечего дайджестить
MAX_DIGEST = 4500          # просим модель уложиться; эпизоды режем по 2500

PROMPT = """Ты — секретарь технической сессии. Ниже транскрипт диалога владельца (русский, на «ты») с ассистентом Claude Code
на сервере {site}. Составь ДАЙДЖЕСТ для долговременной памяти: только итог, без хода рассуждений, без промежуточных
вариантов и без того, что потом переделали.

Форма (Markdown, заголовки ровно такие, пустые разделы пропускай):
## Тема
1–2 предложения: над чем работали.
## Решения и результаты
Список: что решено и сделано, в каком состоянии оставлено. Конкретика обязательна: имена сервисов, серверов (IP),
файлов, команд, чисел, дат. Планы помечай словом «план:».
## Тупики
Список «X не сработало: причина» — что пробовали и НЕ сработало, и почему. Это самое ценное, не сокращай.
## Открытые вопросы
Что не закончено, что ждёт решения владельца.
## Правила и предпочтения владельца
Только явно сказанное владельцем о том, как ему работать, что подтверждать, как отчитываться.

Требования: русский язык; не длиннее {max_chars} символов; только факты из транскрипта, ничего не выдумывать;
значения паролей/токенов/ключей не включать (метки [REDACTED:…] пропускать); даты в виде 2026-09-16;
решения принимает владелец — ассистента называй «ассистент» и не делай его субъектом решений.
{part_note}
=== ТРАНСКРИПТ ===
{text}"""

MERGE_PROMPT = """Ниже несколько дайджестов ЧАСТЕЙ одной сессии (по порядку). Сведи их в ОДИН дайджест той же формы
(## Тема / ## Решения и результаты / ## Тупики / ## Открытые вопросы / ## Правила и предпочтения владельца), убрав повторы
и оставив только конечное состояние (если в поздней части решение изменилось — бери позднее, раннее упомяни в «Тупиках»,
если это была неудачная попытка). Не длиннее {max_chars} символов, русский, без выдумок.

{parts}"""


def llm(api_key, model, prompt, timeout=300):
    req = urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',
                                 data=json.dumps({'model': model, 'messages': [{'role': 'user', 'content': prompt}], 'temperature': 0.2, 'max_tokens': 6000}).encode(),
                                 headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json', 'HTTP-Referer': 'https://ewa.pro', 'X-Title': 'graphiti session digest'})
    for attempt in range(3):
        try:
            r = json.load(urllib.request.urlopen(req, timeout=timeout))
            u = r.get('usage') or {}
            with open(USAGE, 'a') as f:
                f.write(json.dumps({'ts': time.time(), 'tag': 'digest', 'model': model, 'in': u.get('prompt_tokens', 0), 'out': u.get('completion_tokens', 0)}) + '\n')
            return r['choices'][0]['message']['content'].strip()
        except Exception as e:
            if attempt == 2: raise
            print(f'  llm retry {attempt+1}: {e!r}', file=sys.stderr); time.sleep(5 * (attempt + 1))


def split_text(text, limit):
    out, cur = [], ''
    for para in text.split('\n\n'):
        if len(cur) + len(para) + 2 <= limit: cur = (cur + '\n\n' + para) if cur else para
        else:
            if cur: out.append(cur)
            while len(para) > limit: out.append(para[:limit]); para = para[limit:]
            cur = para
    if cur: out.append(cur)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default='/root/.claude/projects/-root'); ap.add_argument('--out', default=os.path.join(HERE, 'sessions_filtered'))
    ap.add_argument('--name', default='episodes_digest.jsonl'); ap.add_argument('--files', default='', help='префиксы id сессий через запятую')
    ap.add_argument('--min-idle-hours', type=float, default=2); ap.add_argument('--model', default=os.environ.get('DIGEST_MODEL', 'anthropic/claude-sonnet-5'))
    ap.add_argument('--site', default='проде 201.51.23.17 (Timeweb, Амстердам)'); ap.add_argument('--group', default='digest')
    ap.add_argument('--dry-run', action='store_true'); ap.add_argument('--force', action='store_true', help='пересчитать даже при том же хеше')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True); os.makedirs(os.path.dirname(STATE), exist_ok=True)
    api_key = os.environ.get('OPENAI_API_KEY') or next((l.split('=', 1)[1].strip() for l in open(os.path.join(HERE, '.env')) if l.startswith('OPENAI_API_KEY=')), None)
    sf = SecretFilter()
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    files = sorted(glob.glob(os.path.join(a.src, '*.jsonl')))
    if a.files: files = [f for f in files if any(os.path.basename(f).startswith(p) for p in a.files.split(','))]
    cutoff = time.time() - a.min_idle_hours * 3600
    files = [f for f in files if os.path.getmtime(f) < cutoff]
    out_path = os.path.join(a.out, a.name); n_new = n_skip = 0; episodes = []; red = collections.Counter()
    if a.files: state = {k: v for k, v in state.items() if any(k.startswith(p) for p in a.files.split(','))} | {}  # выход только по выбранным (state на диске не режем)
    full_state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    for f in files:
        sid = os.path.basename(f)[:8]
        title, turns = turns_of(f)
        if not turns: continue
        clean = []
        for ts, role, text in turns:
            t, st = sf.redact(text); red.update(st); clean.append((ts, role, t))
        text = '\n\n'.join(f'{role}: {t}' for _, role, t in clean)
        if len(text) < MIN_CHARS: continue
        h = hashlib.sha1(text.encode()).hexdigest()
        if not a.force and state.get(sid, {}).get('hash') == h: n_skip += 1; continue
        d0, d1 = clean[0][0][:10], clean[-1][0][:10]
        print(f'[{sid}] «{title}» {d0}…{d1}: {len(turns)} реплик, {len(text)//1000}k симв', flush=True)
        if a.dry_run: n_new += 1; continue
        parts = split_text(text, PART_CHARS)
        digests = []
        for i, p in enumerate(parts):
            note = f'(Это часть {i+1} из {len(parts)} длинной сессии.)' if len(parts) > 1 else ''
            digests.append(llm(api_key, a.model, PROMPT.format(site=a.site, max_chars=MAX_DIGEST, part_note=note, text=p)))
        digest = digests[0] if len(digests) == 1 else llm(api_key, a.model, MERGE_PROMPT.format(max_chars=MAX_DIGEST, parts='\n\n=== ЧАСТЬ ===\n'.join(digests)))
        digest, st = sf.redact(digest); red.update(st)
        # текст дайджеста храним в state/digests/<sid>.md: выходной jsonl каждый раз собирается из ВСЕХ дайджестов
        # (загрузчик сам пропустит уже загруженные имена по state/main.json) — иначе дайджест, посчитанный вне крона
        # (тест, ручной прогон), никогда не попал бы в main
        os.makedirs(DIGEST_DIR, exist_ok=True)
        open(os.path.join(DIGEST_DIR, f'{sid}.md'), 'w').write(digest)
        state[sid] = full_state[sid] = {'hash': h, 'ts': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'), 'chars': len(text),
                                        'title': title, 'from': d0, 'to': d1, 'ref': clean[0][0]}
        json.dump(full_state, open(STATE, 'w'), ensure_ascii=False, indent=1)
        n_new += 1
    if not a.dry_run:
        for sid, st in sorted(state.items()):
            dp = os.path.join(DIGEST_DIR, f'{sid}.md')
            if not os.path.exists(dp): continue
            digest = open(dp).read(); title = st.get('title', ''); d0, d1 = st.get('from', ''), st.get('to', '')
            head = f'[Дайджест сессии Claude Code на {a.site}, тема «{title or "без названия"}», {d0}' + (f'…{d1}' if d1 != d0 else '') + ']\n\n'
            chunks = split_text(digest, 2500 - len(head))
            for i, c in enumerate(chunks):
                episodes.append({'name': f'digest_{sid}@{st["hash"][:8]}' + (f'#{i+1}' if len(chunks) > 1 else ''), 'group': a.group,
                                 'content': head + c, 'reference_time': st.get('ref', ''),
                                 'source_description': f'дайджест сессии {sid} ({d0}…{d1}) «{title}»'})
        with open(out_path, 'w') as fo:
            for e in episodes: fo.write(json.dumps(e, ensure_ascii=False) + '\n')
        os.chmod(out_path, 0o600)
        # контроль утечек по выходу
        leaks = sum(1 for e in episodes for _ in sf.leaks(e['content'])) + sum(1 for e in episodes if sf.known_re and sf.known_re.search(e['content']))
        print(f'дайджестов: {n_new} новых, {n_skip} без изменений; эпизодов={len(episodes)}; замен фильтра {sum(red.values())}; утечек после фильтра: {leaks} → {out_path}')
        if leaks: sys.exit(1)
    else:
        print(f'dry-run: {n_new} сессий к дайджесту, {n_skip} без изменений')


if __name__ == '__main__':
    main()
