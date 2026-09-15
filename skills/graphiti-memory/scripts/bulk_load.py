#!/usr/bin/env python3
"""Массовая загрузка файлов памяти (*.md с frontmatter) в Graphiti через graphiti-core (add_episode_bulk), минуя MCP-очередь.
Запуск ВНУТРИ контейнера (там нужная версия graphiti-core и доступ к FalkorDB); env даёт `gm_config.py shell` → GM_DOCKER_ENV:
  docker exec $GM_DOCKER_ENV -e OPENAI_API_KEY=… graphiti-mcp /app/mcp/.venv/bin/python /app/loaders/bulk_load.py \
      --source memory [--group main] [--batch 12] [--hash-names] [--limit N] [--dry-run]
  --source: ключ источника из graphiti-memory.yaml (`memory` или label из `sources.archives`) ИЛИ путь к каталогу с *.md.
Идемпотентно: имена загруженных эпизодов — в state/<group>.json (том ./state смонтирован rw). Один загрузчик на группу (flock).
Все серверные настройки (модели, инструкция, исключения, манифест mtime) — из graphiti-memory.yaml; env перекрывает модели/URL.
"""
import argparse, asyncio, datetime, glob, hashlib, json, os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yaml
from gm_config import cfg
import embed_chunk_patch  # noqa: F401  — локальный TEI принимает ≤32 текстов за запрос
if os.environ.get('VECTOR_INDEX', '0') == '1':
    import falkor_vector_patch  # дедуп через векторный индекс FalkorDB вместо полного скана
else:
    falkor_vector_patch = None
from pydantic import create_model
from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver
from falkordb.asyncio import FalkorDB
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.nodes import EpisodeType
from graphiti_core.utils.bulk_utils import RawEpisode


def chunks(text, limit=2500):
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


def parse_fm(t):
    m = re.match(r'^---\n(.*?)\n---\n', t, re.S)
    if not m: return {}, t
    fm = {}
    for line in m.group(1).splitlines():
        k = re.match(r'^(\w+):\s*(.*)$', line)
        if k: fm[k.group(1)] = k.group(2).strip().strip('"')
    return fm, t[m.end():]


def env_or_cfg(c, env, key, default=None):
    v = os.environ.get(env)
    return v if v not in (None, '') else (c.get(key) if c.get(key) is not None else default)


def make_clients(api_key, base, model, emb_model, emb_dim, emb_base):
    """LLM/эмбеддер/реранкер с СОБСТВЕННЫМИ SDK-клиентами: keep-alive напрямую к провайдеру (прокси без keep-alive
    добавлял ~30–50 % к каждому вызову), таймауты и ретраи в клиенте: LLM_TIMEOUT (120 с), EMB_TIMEOUT (20 с), LLM_RETRIES (3).
    small_model — дедуп/инвалидация (ModelSize.small)."""
    import httpx
    from openai import AsyncOpenAI
    small = os.environ.get('SMALL_MODEL_NAME') or model
    llm_client = AsyncOpenAI(api_key=api_key, base_url=base, max_retries=int(os.environ.get('LLM_RETRIES', '3')),
                             timeout=httpx.Timeout(float(os.environ.get('LLM_TIMEOUT', '120')), connect=10.0))
    emb_client = AsyncOpenAI(api_key=api_key, base_url=emb_base, max_retries=4,
                             timeout=httpx.Timeout(float(os.environ.get('EMB_TIMEOUT', '20')), connect=10.0))
    llm = OpenAIGenericClient(config=LLMConfig(api_key=api_key, model=model, small_model=small, base_url=base), max_tokens=8192, client=llm_client)
    embedder = OpenAIEmbedder(config=OpenAIEmbedderConfig(embedding_model=emb_model, embedding_dim=emb_dim, api_key=api_key, base_url=emb_base), client=emb_client)
    reranker = OpenAIRerankerClient(config=LLMConfig(api_key=api_key, model=model, base_url=base), client=llm_client)
    print(f'llm={model} small_model={small} @ {base} (timeout {llm_client.timeout}); embedder={emb_model} @ {emb_base} (timeout {emb_client.timeout})', flush=True)
    return llm, embedder, reranker


def doc_only_types(cfg_path):
    """Все типы из config.yaml (MCP) как doc-only модели (без атрибутов → без лишних LLM-вызовов на сущность)."""
    d = yaml.safe_load(open(cfg_path))
    out = {}
    for e in (d.get('graphiti', {}).get('entity_types') or []):
        m = create_model(e['name']); m.__doc__ = e['description']; out[e['name']] = m
    return out or None


def open_graphiti(c, api_key, base, model, emb_model, emb_dim, emb_base):
    llm, embedder, reranker = make_clients(api_key, base, model, emb_model, emb_dim, emb_base)
    # свой клиент с большим пулом: дефолтный пул redis-py упирается в «Too many connections» при SEMAPHORE_LIMIT≥20
    fdb = FalkorDB(host=os.environ.get('FALKORDB_HOST', 'localhost'), port=6379, max_connections=int(os.environ.get('FALKOR_MAX_CONN', '512')))
    driver = FalkorDriver(falkor_db=fdb, database=os.environ.get('FALKORDB_DATABASE', 'default_db'))
    return Graphiti(graph_driver=driver, llm_client=llm, embedder=embedder, cross_encoder=reranker, max_coroutines=int(os.environ.get('SEMAPHORE_LIMIT', '20')))


def take_lock(state_dir, group):
    import fcntl  # одна группа — один загрузчик: второй экземпляр выходит сразу (иначе дубли за батч)
    lock = open(f'{state_dir}/{group}.lock', 'w')
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: print(f'[{group}] уже грузится другим процессом — выхожу', flush=True); return None
    return lock


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True, help='ключ источника (memory | label архива) или каталог с *.md')
    ap.add_argument('--group', default=None, help='group_id (= имя графа); по умолчанию graph из конфига')
    ap.add_argument('--batch', type=int, default=None); ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--offset', type=int, default=0, help='пропустить первые N новых эпизодов')
    ap.add_argument('--skip-state', default='', help='группы через запятую, чьи state считать уже загруженными')
    ap.add_argument('--files', default='', help='список имён через запятую (иначе все *.md)')
    ap.add_argument('--exclude', default=None, help='имена файлов через запятую, которые не грузить (по умолчанию из конфига источника)')
    ap.add_argument('--manifest', default=None, help='JSON {file: {mtime_epoch}} с исходными mtime (по умолчанию из конфига архива)')
    ap.add_argument('--instr', default=None, help='инструкция извлечения: ключ instructions.* или текст (по умолчанию instructions.memory)')
    ap.add_argument('--mcp-config', default='/app/mcp/config/config.yaml', help='config.yaml MCP — типы сущностей')
    ap.add_argument('--config', default=None, help='graphiti-memory.yaml')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--hash-names', action='store_true', help='имя эпизода = файл#i@sha1(текст)[:8]: изменённый кусок грузится заново (для докачки)')
    a = ap.parse_args()
    c = cfg(a.config)
    group = a.group or c.graph; batch = a.batch or int(c.get('load.batch', 12)); limit_chars = int(c.get('load.chunk_limit', 2500))

    src = None
    if os.path.isdir(a.source): source_dir = a.source
    else: src = c.source(a.source); source_dir = src['mount'] if c.inside else src['path']
    exclude = set(filter(None, (a.exclude if a.exclude is not None else ','.join((src or {}).get('exclude', []) or [])).split(',')))
    skip_index = set((src or {}).get('skip_index') or c.get('sources.memory.skip_index', ['MEMORY.md', 'memory_ext.md', '_archived_index.md']) or [])
    manifest_path = a.manifest if a.manifest is not None else ((src or {}).get('manifest') and os.path.join(source_dir, src['manifest']))
    hist_note = (src or {}).get('historical_note') or '[АРХИВ, написано {written_at}] '
    instr = a.instr if a.instr is not None else 'memory'
    if instr in (c.get('instructions') or {}): instr = c.instruction(instr)

    api_key = os.environ['OPENAI_API_KEY']; base = env_or_cfg(c, 'OPENAI_API_URL', 'llm.api_url', 'https://openrouter.ai/api/v1')
    model = env_or_cfg(c, 'MODEL_NAME', 'llm.model'); emb_model = env_or_cfg(c, 'EMBEDDER_MODEL', 'embedder.model')
    emb_dim = int(env_or_cfg(c, 'EMBEDDER_DIMENSIONS', 'embedder.dimensions', 1024))
    # 🔴 база эмбеддера — СВОЯ переменная (EMBEDDER_API_URL), не base LLM: иначе 20k вызовов уходят не туда (грабля 15.09)
    emb_base = env_or_cfg(c, 'EMBEDDER_API_URL', 'embedder.bulk_api_url', base)
    if not os.environ.get('SMALL_MODEL_NAME') and c.get('llm.small_model'): os.environ['SMALL_MODEL_NAME'] = c.get('llm.small_model')
    state_dir = c.state_dir(); os.makedirs(state_dir, exist_ok=True)
    state_path = f'{state_dir}/{group}.json'
    lock = take_lock(state_dir, group)
    if not lock: return
    done = set(json.load(open(state_path))) if os.path.exists(state_path) else set()
    skip = set()
    for g in filter(None, a.skip_state.split(',')):
        p = f'{state_dir}/{g}.json'
        if os.path.exists(p): skip |= set(json.load(open(p)))
    manifest = json.load(open(manifest_path)) if manifest_path and os.path.exists(manifest_path) else {}

    files = [os.path.join(source_dir, f) for f in a.files.split(',')] if a.files else sorted(glob.glob(os.path.join(source_dir, '*.md')))
    episodes = []
    for p in files:
        raw = open(p, errors='ignore').read(); fm, body = parse_fm(raw)
        base_name = os.path.basename(p)
        if base_name in skip_index or base_name in exclude: continue  # индексы и мета-файлы — не знания
        mt = manifest.get(base_name, {}).get('mtime_epoch') or os.path.getmtime(p)
        ref = datetime.datetime.fromtimestamp(mt, datetime.timezone.utc)
        head = ''
        if fm.get('historical') == 'true': head = hist_note.format(written_at=fm.get('written_at', '?'))
        elif fm.get('status'): head = f"[статус на {fm.get('status_checked', '?')}: {fm['status']}] "
        desc = fm.get('description', '')
        for i, ch in enumerate(chunks(body, limit_chars)):
            name = f'{base_name}#{i+1}'
            if a.hash_names: name += '@' + hashlib.sha1(ch.encode()).hexdigest()[:8]
            if name in done or name in skip: continue
            content = (f'{head}{desc}\n\n' if (i == 0 and desc) else (f'{head}\n\n' if head else '')) + ch
            episodes.append(RawEpisode(name=name, content=content, source_description=f'файл памяти {base_name}', source=EpisodeType.text, reference_time=ref))
    if a.offset: episodes = episodes[a.offset:]
    if a.limit: episodes = episodes[:a.limit]
    print(f'[{group}] источник={source_dir} файлов={len(files)} новых эпизодов={len(episodes)} (уже загружено {len(done)}, пропущено из других групп {len(skip)}) модель={model} эмбеддер={emb_model}/{emb_dim} batch={batch}', flush=True)
    if a.dry_run or not episodes: return

    g = open_graphiti(c, api_key, base, model, emb_model, emb_dim, emb_base)
    await g.build_indices_and_constraints()
    types = doc_only_types(a.mcp_config)
    t_all = time.time(); n_err = 0
    for bi in range(0, len(episodes), batch):
        part = episodes[bi:bi + batch]; t0 = time.time()
        try:
            await g.add_episode_bulk(part, group_id=group, entity_types=types, custom_extraction_instructions=instr)
            done.update(e.name for e in part)
            json.dump(sorted(done), open(state_path, 'w'), ensure_ascii=False)
            print(f'  batch {bi//batch+1}/{(len(episodes)+batch-1)//batch}: {len(part)} эпизодов за {time.time()-t0:.0f}с; всего {len(done)}', flush=True)
        except Exception as e:
            n_err += 1; print(f'  !! batch {bi//batch+1} ошибка: {str(e)[:300]}', flush=True)
    print(f'[{group}] готово за {(time.time()-t_all)/60:.1f} мин, батчей с ошибкой: {n_err}', flush=True)
    if falkor_vector_patch: print(f'[{group}] vector_patch: {dict(falkor_vector_patch.stats)}', flush=True)
    await g.close()


if __name__ == '__main__':
    asyncio.run(main())
