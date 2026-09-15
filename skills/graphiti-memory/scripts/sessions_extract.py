#!/usr/bin/env python3
"""Транскрипты Claude Code (*.jsonl) → эпизоды для Graphiti (на хосте, ДО загрузки).

Берём ТОЛЬКО текст диалога: реплики человека и текстовые ответы ассистента. Выбрасываем: tool_use/tool_result,
thinking, system-reminder'ы, hook-вставки, вставки скиллов, /команды, task-notification, isMeta, sidechain.
Каждая реплика прогоняется через secret_filter (словарь реальных секретов + регулярки + энтропия).
Диалог режется на куски ≤ load.chunk_limit символов по границам реплик; reference_time куска = время первой реплики.
Имя эпизода = <prefix>_<id8>#<i>@sha1(тело)[:8] — растущая сессия меняет лишь последний кусок (докачка идемпотентна).

Источник — из graphiti-memory.yaml: --source transcripts (этот сервер) или label из sources.archive_transcripts.
Выход: <work_dir>/episodes_<source>.jsonl (name, group, content, reference_time, source_description) + report_<source>.json
Проверка: --check — повторно прогоняет фильтр по выходу; утечек должно быть 0 (код возврата 1, если нет).
"""
import argparse, collections, datetime, glob, hashlib, json, os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gm_config import cfg
from secret_filter import SecretFilter

TURN_MAX = 40_000          # реплика длиннее — вставка документа/лога, выбрасываем
STRIP_TAGS = re.compile(r'<(system-reminder|task-notification|local-command-caveat|local-command-stdout|command-name|command-message|command-args|persisted-output)>.*?</\1>', re.S)
SKIP_USER_PREFIX = ('<command-name>', '<local-command', '<task-notification', 'Base directory for this skill', '<persisted-output>')


def turns_of(path):
    """→ (title, [(ts, role, text)])"""
    title = ''; turns = []
    for line in open(path, errors='ignore'):
        try: r = json.loads(line)
        except Exception: continue
        t = r.get('type')
        if t == 'ai-title': title = r.get('aiTitle') or title; continue
        if t not in ('user', 'assistant') or r.get('isMeta') or r.get('isSidechain'): continue
        c = r.get('message', {}).get('content'); ts = r.get('timestamp', '')
        blocks = [c] if isinstance(c, str) else [b.get('text', '') for b in (c or []) if isinstance(b, dict) and b.get('type') == 'text']
        for b in blocks:
            b = STRIP_TAGS.sub('', b).strip()
            if not b or len(b) > TURN_MAX: continue
            if t == 'user' and b.startswith(SKIP_USER_PREFIX): continue
            if t == 'assistant' and len(b) < 15: continue
            turns.append((ts, 'Владелец' if t == 'user' else 'Ассистент', b))
    return title, turns


def split_long(text, limit):
    paras = [p for p in re.split(r'\n\s*\n', text) if p.strip()]
    out, cur = [], ''
    for p in paras:
        if len(cur) + len(p) + 2 <= limit: cur = (cur + '\n\n' + p) if cur else p
        else:
            if cur: out.append(cur)
            while len(p) > limit: out.append(p[:limit]); p = p[limit:]
            cur = p
    if cur: out.append(cur)
    return out


def chunk_turns(turns, limit):
    """Куски ≤limit по границам реплик; длинная реплика режется по абзацам. → [(ts_first, text)]"""
    chunks, cur, cur_ts = [], [], None
    for ts, role, text in turns:
        pieces = [f'{role}: {text}']
        if len(pieces[0]) > limit: pieces = [f'{role}: {p}' if i == 0 else f'{role} (продолжение): {p}' for i, p in enumerate(split_long(text, limit - 30))]
        for piece in pieces:
            if cur and sum(len(x) + 2 for x in cur) + len(piece) > limit:
                chunks.append((cur_ts, '\n\n'.join(cur))); cur, cur_ts = [], None
            if not cur: cur_ts = ts
            cur.append(piece)
    if cur: chunks.append((cur_ts, '\n\n'.join(cur)))
    return chunks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', default='transcripts', help='transcripts | label из sources.archive_transcripts')
    ap.add_argument('--src', default=None, help='каталог с *.jsonl (по умолчанию из конфига источника)')
    ap.add_argument('--out', default=None, help='каталог выхода (по умолчанию <deploy_dir>/sessions_filtered)')
    ap.add_argument('--name', default=None, help='имя выходного файла (по умолчанию episodes_<source>.jsonl)')
    ap.add_argument('--group', default=None, help='поле group в эпизодах (по умолчанию graph из конфига)')
    ap.add_argument('--files', default='', help='список id сессий через запятую (префиксы)')
    ap.add_argument('--min-idle-hours', type=float, default=None, help='пропускать сессии, менявшиеся позже, чем N часов назад (идущие)')
    ap.add_argument('--check', action='store_true', help='только проверить готовый файл эпизодов на утечки')
    ap.add_argument('--config', default=None)
    a = ap.parse_args()
    c = cfg(a.config); src = c.source(a.source)
    out_dir = a.out or c.work_dir(); os.makedirs(out_dir, exist_ok=True); os.chmod(out_dir, 0o700)
    ep_path = os.path.join(out_dir, a.name or f'episodes_{a.source}.jsonl')
    group = a.group or c.graph; limit = int(c.get('load.chunk_limit', 2500))
    header = src.get('header') or '[Транскрипт сессии Claude Code на {server}, тема «{title}», {date}, часть {part}]'
    prefix = src.get('name_prefix', 'sess'); server = c.server_name
    min_idle = a.min_idle_hours if a.min_idle_hours is not None else float(src.get('min_idle_hours', 0) or 0)
    sf = SecretFilter(config=a.config)
    print(f'словарь секретов: {len(sf.known)} значений из {sf.n_files} файлов', file=sys.stderr)

    if a.check:
        leaks = collections.Counter(); n = 0
        for line in open(ep_path):
            n += 1; e = json.loads(line)
            for l in sf.leaks(e['content']): leaks[l] += 1
            for v in sf.known_sorted:  # словарь — отдельно и по-честному
                if v in e['content']: leaks['known!'] += 1; break
        print(f'эпизодов {n}, утечек: {dict(leaks) or "нет"}'); sys.exit(1 if leaks else 0)

    files = sorted(glob.glob(os.path.join(a.src or src['path'], '*.jsonl')))
    if a.files: files = [f for f in files if any(os.path.basename(f).startswith(p) for p in a.files.split(','))]
    if min_idle:
        cutoff = time.time() - min_idle * 3600
        files = [f for f in files if os.path.getmtime(f) < cutoff]
    stats_rules = collections.Counter(); report = []; n_ep = 0
    with open(ep_path, 'w') as fo:
        for f in files:
            sid = os.path.basename(f)[:8]
            title, turns = turns_of(f)
            if not turns: report.append({'session': sid, 'skipped': 'нет текста'}); continue
            clean_turns = []; st = collections.Counter()
            for ts, role, text in turns:
                text, s = sf.redact(text); st.update(s); clean_turns.append((ts, role, text))
            stats_rules.update(st)
            chunks = chunk_turns(clean_turns, limit)
            d0 = clean_turns[0][0][:10]; d1 = clean_turns[-1][0][:10]
            for i, (ts, text) in enumerate(chunks):
                # заголовок БЕЗ «/N» и хеш только по телу: растущая сессия меняет лишь последний кусок
                head = header.format(server=server, title=title or 'без названия', date=ts[:10], part=i + 1) + '\n\n'
                name = f'{prefix}_{sid}#{i+1}@{hashlib.sha1(text.encode()).hexdigest()[:8]}'
                fo.write(json.dumps({'name': name, 'group': group, 'content': head + text, 'reference_time': ts,
                                     'source_description': f'транскрипт сессии {sid} ({d0}…{d1}) «{title}»'}, ensure_ascii=False) + '\n')
            n_ep += len(chunks)
            report.append({'session': sid, 'title': title, 'from': d0, 'to': d1, 'turns': len(turns), 'chunks': len(chunks),
                           'chars': sum(len(t[2]) for t in clean_turns), 'redactions': dict(st)})
    os.chmod(ep_path, 0o600)
    summary = {'source': a.source, 'sessions': len(files), 'episodes': n_ep, 'group': group, 'redactions_by_rule': dict(stats_rules), 'per_session': report}
    json.dump(summary, open(os.path.join(out_dir, f'report_{a.source}.json'), 'w'), ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in summary.items() if k != 'per_session'}, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
