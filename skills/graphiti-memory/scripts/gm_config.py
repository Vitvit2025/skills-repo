#!/usr/bin/env python3
"""Единый конфиг скилла graphiti-memory (graphiti-memory.yaml): всё серверное — пути, модели, псевдонимы, инструкции —
лежит здесь, скрипты ничего не зашивают.

Поиск файла конфига (первый найденный):
  1. --config <путь> / переменная GRAPHITI_MEMORY_CONFIG
  2. /app/conf/graphiti-memory.yaml            — внутри контейнера (каталог ./conf смонтирован как /app/conf)
  3. <deploy_dir>/conf/graphiti-memory.yaml    — рядом со scripts/ (../conf/)
  4. ./conf/graphiti-memory.yaml, ./graphiti-memory.yaml

Как модуль:   from gm_config import cfg, C;  C.graph, C.get('load.batch', 12), C.src('memory'), C.inside
CLI:
  gm_config.py shell            → строки `GM_…=…` для `eval "$(gm_config.py shell)"` в bash (без секретов)
  gm_config.py get load.batch   → одно значение
  gm_config.py steps            → порядок сборки единого графа (kind key), по строке на шаг
  gm_config.py render-compose   → docker-compose.yml по конфигу (тома под источники)
  gm_config.py check            → проверка путей/ключей конфига
"""
import os, sys, json, glob, shlex

try:
    import yaml
except ImportError:  # venv без pyyaml → пробуем системный python3-yaml (apt), иначе просим поставить
    for p in glob.glob('/usr/lib/python3*/dist-packages') + glob.glob('/usr/lib/python3*/site-packages'):
        if p not in sys.path: sys.path.append(p)
    try:
        import yaml
    except ImportError:
        sys.exit('нужен pyyaml: pip install pyyaml (на хосте: apt install python3-yaml)')

INSIDE = os.path.isdir('/app/mcp')  # признак «мы внутри контейнера graphiti-mcp»


def _find(path=None):
    cands = [path, os.environ.get('GRAPHITI_MEMORY_CONFIG'), '/app/conf/graphiti-memory.yaml',
             os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'conf', 'graphiti-memory.yaml'),
             'conf/graphiti-memory.yaml', 'graphiti-memory.yaml']
    for c in cands:
        if c and os.path.isfile(c): return os.path.abspath(c)
    sys.exit('graphiti-memory.yaml не найден: укажи --config или GRAPHITI_MEMORY_CONFIG')


class Config:
    def __init__(self, path=None):
        self.path = _find(path)
        self.d = yaml.safe_load(open(self.path)) or {}
        self.inside = INSIDE

    def get(self, dotted, default=None):
        cur = self.d
        for k in dotted.split('.'):
            if not isinstance(cur, dict) or k not in cur: return default
            cur = cur[k]
        return cur

    # --- часто нужное ---
    @property
    def graph(self): return self.get('graph', 'main')
    @property
    def container(self): return self.get('container', 'graphiti-mcp')
    @property
    def deploy_dir(self): return self.get('deploy_dir', os.path.dirname(os.path.dirname(self.path)))
    @property
    def server_name(self): return self.get('server.name', self.get('server.ip', 'сервер'))

    def state_dir(self):
        return '/app/tools/state' if self.inside else os.path.join(self.deploy_dir, 'state')

    def work_dir(self):
        """каталог отфильтрованных эпизодов (sessions_filtered)"""
        return '/data/sessions_filtered' if self.inside else os.path.join(self.deploy_dir, 'sessions_filtered')

    def source(self, key):
        """Описание источника: sources.<key> или элемент списка sources.archives / sources.archive_transcripts по label."""
        s = self.get(f'sources.{key}')
        if isinstance(s, dict): return s
        for lst in ('archives', 'archive_transcripts'):
            for it in self.get(f'sources.{lst}', []) or []:
                if it.get('label') == key: return it
        sys.exit(f'источник {key!r} не описан в {self.path}')

    def src(self, key):
        """Путь источника: внутри контейнера — точка монтирования, на хосте — путь хоста."""
        s = self.source(key)
        return s['mount'] if self.inside else s['path']

    def instruction(self, key):
        """Инструкция извлечения с подстановкой {server} / {ip}."""
        t = self.get(f'instructions.{key}', '') or ''
        return t.format(server=self.server_name, ip=self.get('server.ip', ''))

    def docker_env(self, include_key=False):
        """Строка `-e K=V …` для docker exec загрузчиков (ключ API — отдельно, из env-файла)."""
        e = {
            'OPENAI_API_URL': self.get('llm.api_url', 'https://openrouter.ai/api/v1'),
            'MODEL_NAME': self.get('llm.model'), 'SMALL_MODEL_NAME': self.get('llm.small_model') or self.get('llm.model'),
            'EMBEDDER_MODEL': self.get('embedder.model'), 'EMBEDDER_DIMENSIONS': self.get('embedder.dimensions', 1024),
            'EMBEDDER_API_URL': self.get('embedder.bulk_api_url') or self.get('llm.api_url'),
            'SEMAPHORE_LIMIT': self.get('load.semaphore', 60), 'VECTOR_INDEX': '1' if self.get('load.vector_index', True) else '0',
            'FALKOR_MAX_CONN': self.get('load.falkor_max_conn', 4096), 'LLM_TIMEOUT': self.get('load.llm_timeout', 120),
            'EMB_TIMEOUT': self.get('load.emb_timeout', 20), 'GRAPHITI_MEMORY_CONFIG': '/app/conf/graphiti-memory.yaml',
        }
        return ' '.join(f'-e {k}={shlex.quote(str(v))}' for k, v in e.items() if v not in (None, ''))

    def mounts(self):
        """Тома compose по конфигу: источники + служебные каталоги."""
        vols = ['./conf:/app/conf:ro', './scripts:/app/loaders:ro', './sessions_filtered:/data/sessions_filtered:ro', './state:/app/tools/state']
        # в контейнер попадают только каталоги памяти (*.md); транскрипты/inbox фильтруются на хосте и заходят готовым jsonl
        seen = set()
        s = self.get('sources.memory')
        if s and s.get('path') and s.get('mount'):
            vols.append(f"{s['path']}:{s['mount']}:ro"); seen.add(s['mount'])
        for it in self.get('sources.archives', []) or []:
            if it.get('mount') and it['mount'] not in seen:
                vols.append(f"{it['path']}:{it['mount']}:ro"); seen.add(it['mount'])
        return vols


_cfg = None


def cfg(path=None):
    global _cfg
    if _cfg is None or (path and _cfg.path != os.path.abspath(path)): _cfg = Config(path)
    return _cfg


class _Lazy:  # C.graph без явного вызова cfg()
    def __getattr__(self, n): return getattr(cfg(), n)


C = _Lazy()


def _shell(c):
    out = {
        'GM_CONFIG': c.path, 'GM_DEPLOY_DIR': c.deploy_dir, 'GM_CONTAINER': c.container, 'GM_GRAPH': c.graph,
        'GM_ENV_FILE': os.path.join(c.deploy_dir, c.get('llm.env_file', '.env')), 'GM_KEY_VAR': c.get('llm.api_key_var', 'OPENAI_API_KEY'),
        'GM_BATCH': c.get('load.batch', 12), 'GM_SEMAPHORE': c.get('load.semaphore', 60), 'GM_DOCKER_ENV': c.docker_env(),
        'GM_PY': c.get('container_python', '/app/mcp/.venv/bin/python'), 'GM_LOADERS': '/app/loaders',
        'GM_STATE_DIR': os.path.join(c.deploy_dir, 'state'), 'GM_WORK_DIR': os.path.join(c.deploy_dir, 'sessions_filtered'),
        'GM_HOST_PY': c.get('host_python', 'python3'), 'GM_ALERT_ENV': c.get('alerts.env_file', ''),
        'GM_ALERT_TOKEN_VAR': c.get('alerts.token_var', 'BOT_TOKEN'), 'GM_ALERT_CHAT_VAR': c.get('alerts.chat_var', 'ALERT_CHAT_ID'),
        'GM_MIN_IDLE_HOURS': c.get('sources.transcripts.min_idle_hours', 2), 'GM_FALKOR_PORT': c.get('ports.falkordb', 6379),
        'GM_LOG_FILTER': c.get('load.log_filter', 'not found in nodes|invalid duplicate|Unterminated|unknown entity'),
        'GM_MEMORY_MOUNT': c.get('sources.memory.mount', ''), 'GM_MEMORY_EXCLUDE': ','.join(c.get('sources.memory.exclude', []) or []),
        'GM_HAS_TRANSCRIPTS': '1' if c.get('sources.transcripts.path') else '', 'GM_HAS_INBOX': '1' if c.get('sources.inbox.path') else '',
    }
    for k, v in out.items(): print(f'{k}={shlex.quote(str(v))}')


def _compose(c):
    p = c.get('ports', {}) or {}
    embed_url = c.get('embedder.mcp_api_url') or c.get('embedder.bulk_api_url') or c.get('llm.api_url')
    y = {
        'services': {'graphiti': {
            'image': c.get('image', 'zepai/knowledge-graph-mcp:latest'), 'container_name': c.container, 'restart': 'unless-stopped',
            'env_file': c.get('llm.env_file', '.env'),
            'environment': ['BROWSER=1', 'FALKORDB_URI=redis://localhost:6379', 'FALKORDB_DATABASE=default_db',
                            'CONFIG_PATH=/app/mcp/config/config.yaml', 'GRAPHITI_TELEMETRY_ENABLED=false',
                            f"GRAPHITI_GROUP_ID={c.graph}", f"MODEL_NAME={c.get('llm.mcp_model') or c.get('llm.model')}",
                            f"OPENAI_API_URL={c.get('llm.mcp_api_url') or c.get('llm.api_url')}",
                            f"EMBEDDER_MODEL={c.get('embedder.model')}", f"EMBEDDER_DIMENSIONS={c.get('embedder.dimensions', 1024)}",
                            f"EMBEDDER_API_URL={embed_url}", f"SEMAPHORE_LIMIT={c.get('mcp.semaphore', 5)}"],
            'volumes': ['falkordb_data:/var/lib/falkordb/data', 'mcp_logs:/var/log/graphiti', './config.yaml:/app/mcp/config/config.yaml:ro'] + c.mounts(),
            'ports': [f"127.0.0.1:{p.get('falkordb', 6379)}:6379", f"127.0.0.1:{p.get('ui', 3001)}:3000", f"127.0.0.1:{p.get('mcp', 8000)}:8000"],
            'healthcheck': {'test': ['CMD', 'redis-cli', '-p', '6379', 'ping'], 'interval': '10s', 'timeout': '5s', 'retries': 5, 'start_period': '15s'},
        }},
        'volumes': {'falkordb_data': {}, 'mcp_logs': {}},
    }
    print('# сгенерировано gm_config.py render-compose из', c.path, '— порты ТОЛЬКО 127.0.0.1; каталоги монтируются каталогами (не файлами)')
    print(yaml.safe_dump(y, allow_unicode=True, sort_keys=False))


def _check(c):
    ok = True
    def p(msg, good=True):
        nonlocal ok; ok &= good; print(('✅ ' if good else '❌ ') + msg)
    p(f'конфиг {c.path}')
    for k in ('graph', 'container', 'deploy_dir', 'llm.model', 'embedder.model', 'embedder.dimensions', 'sources.memory.path'):
        p(f'{k} = {c.get(k)!r}', c.get(k) not in (None, ''))
    env = os.path.join(c.deploy_dir, c.get('llm.env_file', '.env'))
    p(f'env-файл {env} (ключ {c.get("llm.api_key_var", "OPENAI_API_KEY")})', os.path.isfile(env))
    if os.path.isfile(env): p(f'права env-файла 600', oct(os.stat(env).st_mode & 0o777) == '0o600')
    for key in ['memory', 'transcripts', 'inbox']:
        s = c.get(f'sources.{key}')
        if s and s.get('path'): p(f'источник {key}: {s["path"]}', os.path.exists(s['path']))
    for lst in ('archives', 'archive_transcripts'):
        for it in c.get(f'sources.{lst}', []) or []:
            p(f'{lst}/{it.get("label")}: {it.get("path")}', os.path.exists(it.get('path', '')))
    n = sum(len(glob.glob(g)) for g in c.get('secrets.files', []) or [])
    p(f'secrets.files: {len(c.get("secrets.files", []) or [])} шаблонов → {n} файлов на хосте', True)
    p(f'aliases: {len(c.get("aliases", {}) or {})} канонических имён', True)
    for k in ('memory', 'transcripts', 'inbox'):
        p(f'instructions.{k}: {len(c.get(f"instructions.{k}", "") or "")} симв.', bool(c.get(f'instructions.{k}')))
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument('cmd', choices=['shell', 'get', 'steps', 'render-compose', 'check', 'dump'])
    ap.add_argument('key', nargs='?'); ap.add_argument('--config'); a = ap.parse_args()
    c = cfg(a.config)
    if a.cmd == 'shell': _shell(c)
    elif a.cmd == 'get': v = c.get(a.key); print(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else ('' if v is None else v))
    elif a.cmd == 'steps':  # kind key instr — по строке на шаг сборки
        for s in c.get('build.steps', []) or []:
            kind = s.get('kind'); key = s.get('key', '') or kind
            src = c.get(f'sources.{kind}') if kind in ('memory', 'transcripts', 'inbox') else c.source(key)
            instr = s.get('instr') or (src or {}).get('instruction') or {'memory': 'memory', 'archive': 'memory', 'transcripts': 'transcripts',
                                                                          'archive_transcripts': 'transcripts_archive', 'inbox': 'inbox'}[kind]
            print(kind, key, instr)
    elif a.cmd == 'render-compose': _compose(c)
    elif a.cmd == 'check': _check(c)
    elif a.cmd == 'dump': print(json.dumps(c.d, ensure_ascii=False, indent=1))
