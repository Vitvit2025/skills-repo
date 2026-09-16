#!/usr/bin/env python3
"""Файлы памяти прода → копия через secret_filter (16.09.2026): загрузчик работает в контейнере, где нет словаря
секретов хоста, поэтому фильтруем на хосте и грузим из очищенной копии. Без этого каждая перезагрузка памяти тянула в
граф base64-ключи/ID (entropy/hex32) и scrub_graph слал алерт «докачка пропустила секреты» (11 полей 16.09).
Имена и mtime файлов сохраняются (имя = ключ state, mtime = reference_time). kv_ru для прозы выключено (шумит).
  python3 memory_clean.py [--src ~/.claude/projects/-root/memory] [--dst sessions_filtered/memory_clean]"""
import argparse, glob, os, shutil, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from secret_filter import SecretFilter

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--src', default='/root/.claude/projects/-root/memory'); ap.add_argument('--dst', default=os.path.join(HERE, 'sessions_filtered', 'memory_clean'))
    a = ap.parse_args(); os.makedirs(a.dst, exist_ok=True); os.chmod(a.dst, 0o700)
    sf = SecretFilter(); n = red = 0; per = {}
    for old in glob.glob(os.path.join(a.dst, '*.md')): os.unlink(old)   # удалённые заметки не должны оставаться
    for f in sorted(glob.glob(os.path.join(a.src, '*.md'))):
        t = open(f, errors='ignore').read(); clean, st = sf.redact(t, exclude=('kv_ru',))
        dst = os.path.join(a.dst, os.path.basename(f)); open(dst, 'w').write(clean); shutil.copystat(f, dst); os.chmod(dst, 0o600)
        k = sum(st.values()); n += 1; red += k
        if k: per[os.path.basename(f)] = {r: v for r, v in st.items() if v}
    print(f'память: файлов {n}, замен {red} → {a.dst}' + (f'; по файлам: {per}' if per else ''))


if __name__ == '__main__':
    main()
