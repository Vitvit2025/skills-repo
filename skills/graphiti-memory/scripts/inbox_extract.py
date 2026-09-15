#!/usr/bin/env python3
"""Текстовые сообщения владельца из Telegram-бота-инбокса (jsonl: id, ts, kind, text|caption, file) → эпизоды для Graphiti.
Берутся только записи с текстом/подписью длиннее sources.inbox.min_chars (файлы и фото без текста — нет). Секреты — через secret_filter.
Выход: <work_dir>/episodes_inbox.jsonl, имя эпизода inbox_<id>@<hash>, group = graph из конфига."""
import argparse, hashlib, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gm_config import cfg
from secret_filter import SecretFilter


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default=None); ap.add_argument('--out', default=None)
    ap.add_argument('--group', default=None); ap.add_argument('--config', default=None); a = ap.parse_args()
    c = cfg(a.config); ib = c.get('sources.inbox') or {}
    src = a.src or ib.get('path'); out_dir = a.out or c.work_dir(); group = a.group or c.graph
    bot = ib.get('bot', 'inbox-бот'); min_chars = int(ib.get('min_chars', 20))
    exclude = tuple(c.get('secrets.exclude_rules_prose', ['kv_ru']) or [])
    if not src or not os.path.exists(src): sys.exit(f'inbox: файл не найден: {src}')
    os.makedirs(out_dir, exist_ok=True)
    sf = SecretFilter(config=a.config); n = 0; red = 0
    out = os.path.join(out_dir, 'episodes_inbox.jsonl')
    with open(out, 'w') as fo:
        for line in open(src, errors='ignore'):
            try: r = json.loads(line)
            except Exception: continue
            text = (r.get('text') or '').strip() or (r.get('caption') or '').strip()
            if len(text) < min_chars: continue
            clean, st = sf.redact(text, exclude=exclude); red += sum(st.values())
            body = f"[Сообщение владельца в inbox-бот {bot}, {r.get('ts', '')[:16]}, тип: {r.get('kind', '')}" + \
                   (f", файл: {os.path.basename(r['file'])}" if r.get('file') else '') + "]\n\n" + clean
            fo.write(json.dumps({'name': f"inbox_{r.get('id')}@{hashlib.sha1(clean.encode()).hexdigest()[:8]}", 'group': group,
                                 'content': body, 'reference_time': r.get('ts', ''), 'source_description': f'inbox {bot}'}, ensure_ascii=False) + '\n')
            n += 1
    os.chmod(out, 0o600)
    print(f'inbox: эпизодов {n}, замен фильтра {red} → {out}')


if __name__ == '__main__':
    main()
