#!/usr/bin/env python3
"""Массовая загрузка файлов памяти в Graphiti через graphiti-core (add_episode_bulk), минуя MCP-очередь.
Запуск ВНУТРИ контейнера graphiti-mcp (там нужная версия graphiti-core и доступ к FalkorDB):
  docker exec -e SEMAPHORE_LIMIT=20 graphiti-mcp /app/mcp/.venv/bin/python /app/tools/bulk_load.py \
      --source /data/memory --group mem_prod [--batch 15] [--limit N] [--dry-run] [--manifest /data/devmem/_devmem_manifest.json]
Идемпотентно: имена загруженных эпизодов хранятся в /app/tools/state/<group>.json (том ./tools смонтирован rw).
Стоимость считается прокси (тег bulk, порт 18004) — см. or_usage.jsonl на хосте.
"""
import argparse, asyncio, datetime, glob, json, os, re, sys, time
import yaml
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import embed_chunk_patch  # noqa: F401  — локальный TEI принимает ≤32 текстов за запрос
if os.environ.get('VECTOR_INDEX', '0') == '1':
    import falkor_vector_patch  # дедуп через векторный индекс FalkorDB вместо полного скана
else:
    falkor_vector_patch = None
from pydantic import BaseModel, create_model
from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver
from falkordb.asyncio import FalkorDB
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.nodes import EpisodeType
from graphiti_core.utils.bulk_utils import RawEpisode

INSTR = ("Текст — русские рабочие заметки админа об инфраструктуре и проектах владельца. "
 "IP-адрес, hostname и разговорное имя одной машины (например «прод», «201.51.23.17», «Амстердам») — ОДНА сущность Server; "
 "называй её по IP, прозвище — в summary. Не создавай сущности для путей к файлам, имён файлов памяти (*.md), кодов ошибок, имён переменных окружения и сетевых интерфейсов — это не сущности; скрипты и ранбуки с именем — можно. "
 "Никогда не извлекай значения секретов (пароли, токены, ключи) — только их названия и где лежат. "
 "Не делай сущностями порты, сетевые интерфейсы, числовые идентификаторы серверов у провайдера, адреса вида host:port, форматы данных. "
 "Даты вида 26.07 или 14.09 относятся к 2026 году. invalid_at ставь ТОЛЬКО если в тексте явно сказано, что факт перестал быть верным "
 "(«закрыто», «больше не», «устарело», «до <дата>», «заменили на», «раньше … теперь …»); не выдумывай даты окончания и не ставь invalid_at по дате файла. "
 "Если в шапке файла стоит historical: true / баннер «АРХИВ dev до переезда» — факты о ролях серверов действовали до 2026-07-26.")

# Общие правила извлечения для ВСЕХ источников (16.09.2026, после аудита графа): нарицательные не сущности, люди с ролью,
# книги — Publication, планы отличать от фактов. Загрузчики добавляют это к своей инструкции.
INSTR_COMMON = (" Не создавай сущности из нарицательных слов без собственного имени («бот», «банк», «сервер», «база», «скрипт», «проект») — "
 "используй конкретное имя из текста; если конкретного имени нет, факт привязывай к ближайшей НАЗВАННОЙ сущности или не извлекай. "
 "Человека называй с уточнением роли, если фамилия распространённая или в тексте он назван только фамилией: «Пётр Баранов (автор пособий ЕГЭ)». "
 "Книги, пособия, методички, спецификации — тип Publication (автор + название), не Company и не Human. "
 "Планы и намерения («планируем», «надо», «предлагаю») извлекай как факты с пометкой «план»; не выдавай план за сделанное. "
 "Неудачную попытку («пробовали X — не сработало из-за Y») извлекай как факт TRIED_AND_FAILED с причиной — это ценное знание. "
 "Для связи выбирай тип из FACT_TYPES, если подходит (RUNS_ON, STORES_DATA_IN, USES, DEPENDS_ON, …); один и тот же смысл всегда одним типом.")

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

def make_clients(api_key, base, model, emb_model, emb_dim, emb_base):
    """LLM/эмбеддер/реранкер с СОБСТВЕННЫМИ SDK-клиентами: keep-alive к OpenRouter напрямую (прокси без keep-alive
    добавлял ~30–50 % к каждому вызову), таймауты и ретраи в клиенте (вместо обрезки на прокси):
    LLM_TIMEOUT (120 с), EMB_TIMEOUT (20 с), LLM_RETRIES (3). small_model — дедуп/инвалидация (ModelSize.small)."""
    import httpx
    from openai import AsyncOpenAI
    small = os.environ.get('SMALL_MODEL_NAME') or model
    llm_client = AsyncOpenAI(api_key=api_key, base_url=base, max_retries=int(os.environ.get('LLM_RETRIES', '3')),
                             timeout=httpx.Timeout(float(os.environ.get('LLM_TIMEOUT', '120')), connect=10.0))
    emb_client = AsyncOpenAI(api_key=api_key, base_url=emb_base, max_retries=4,
                             timeout=httpx.Timeout(float(os.environ.get('EMB_TIMEOUT', '20')), connect=10.0))
    # учёт токенов ПО МОДЕЛЯМ (16.09.2026): bulk идёт в OpenRouter напрямую, мимо учётного прокси → пишем usage сами
    usage_path = os.environ.get('USAGE_LOG', '/app/tools/state/usage.jsonl'); tag = os.environ.get('USAGE_TAG', 'bulk')
    _orig_create = llm_client.chat.completions.create
    async def _create_logged(*args, **kw):
        resp = await _orig_create(*args, **kw)
        try:
            u = getattr(resp, 'usage', None)
            if u:
                with open(usage_path, 'a') as f:
                    f.write(json.dumps({'ts': time.time(), 'tag': tag, 'model': kw.get('model'), 'in': u.prompt_tokens, 'out': u.completion_tokens}) + '\n')
        except Exception: pass
        return resp
    llm_client.chat.completions.create = _create_logged
    llm = OpenAIGenericClient(config=LLMConfig(api_key=api_key, model=model, small_model=small, base_url=base), max_tokens=8192, client=llm_client)
    embedder = OpenAIEmbedder(config=OpenAIEmbedderConfig(embedding_model=emb_model, embedding_dim=emb_dim, api_key=api_key, base_url=emb_base), client=emb_client)
    reranker = OpenAIRerankerClient(config=LLMConfig(api_key=api_key, model=model, base_url=base), client=llm_client)
    print(f'llm={model} small_model={small} @ {base} (timeout {llm_client.timeout}); embedder={emb_model} @ {emb_base} (timeout {emb_client.timeout})', flush=True)
    return llm, embedder, reranker


def doc_only_types(cfg_path):
    """Все типы из config.yaml как doc-only модели (без атрибутов → без лишних LLM-вызовов на сущность)."""
    cfg = yaml.safe_load(open(cfg_path))
    out = {}
    for e in (cfg.get('graphiti', {}).get('entity_types') or []):
        m = create_model(e['name']); m.__doc__ = e['description']; out[e['name']] = m
    return out or None


def doc_only_edge_types(cfg_path):
    """Типы связей из config.yaml как doc-only модели (без полей → extract_attributes не вызывается, см. edge_operations)."""
    cfg = yaml.safe_load(open(cfg_path))
    out = {}
    for e in (cfg.get('graphiti', {}).get('edge_types') or []):
        m = create_model(e['name']); m.__doc__ = e['description']; out[e['name']] = m
    return out or None

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True, help='каталог с *.md')
    ap.add_argument('--group', required=True)
    ap.add_argument('--batch', type=int, default=15)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--offset', type=int, default=0, help='пропустить первые N новых эпизодов (деление корпуса на группы)')
    ap.add_argument('--skip-state', default='', help='группы через запятую, чьи state считать уже загруженными (продолжение корпуса в новой группе)')
    ap.add_argument('--files', default='', help='список имён через запятую (иначе все *.md)')
    ap.add_argument('--manifest', default='', help='_devmem_manifest.json с исходными mtime (для dev-памяти)')
    ap.add_argument('--config', default='/app/mcp/config/config.yaml')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--hash-names', action='store_true', help='имя эпизода = файл#i@sha1(текст)[:8]: изменённый кусок грузится заново, неизменный пропускается (для крон-докачки)')
    ap.add_argument('--exclude', default='', help='имена файлов через запятую, которые не грузить (напр. project_graphiti_memory.md — даёт мета-факты о самом графе)')
    a = ap.parse_args()
    exclude = set(filter(None, a.exclude.split(',')))

    api_key = os.environ['OPENAI_API_KEY']; base = os.environ.get('OPENAI_API_URL', 'http://172.17.0.1:18004/v1')
    model = os.environ.get('MODEL_NAME', 'google/gemini-2.5-flash-lite'); emb_model = os.environ.get('EMBEDDER_MODEL', 'qwen/qwen3-embedding-4b')
    emb_dim = int(os.environ.get('EMBEDDER_DIMENSIONS', '2560'))
    # 🔴 эмбеддер — ЛОКАЛЬНЫЙ TEI (EMBEDDER_API_URL), не прокси OpenRouter: до 15.09 05:55 этот загрузчик по ошибке
    # слал все эмбеддинги в OpenRouter (base) — 20k вызовов, p50 0.5 с, хвосты до 136 с → батчи по 200–500 с
    emb_base = os.environ.get('EMBEDDER_API_URL', 'http://172.17.0.1:18081/v1')
    state_dir = '/app/tools/state'; os.makedirs(state_dir, exist_ok=True)
    state_path = f'{state_dir}/{a.group}.json'
    import fcntl  # одна группа — один загрузчик: второй экземпляр выходит сразу (иначе дубли за батч)
    lock = open(f'{state_dir}/{a.group}.lock', 'w')
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: print(f'[{a.group}] уже грузится другим процессом — выхожу', flush=True); return
    done = set(json.load(open(state_path))) if os.path.exists(state_path) else set()
    skip = set()
    for g in filter(None, a.skip_state.split(',')):
        p = f'{state_dir}/{g}.json'
        if os.path.exists(p): skip |= set(json.load(open(p)))
    manifest = json.load(open(a.manifest)) if a.manifest else {}

    files = [os.path.join(a.source, f) for f in a.files.split(',')] if a.files else sorted(glob.glob(os.path.join(a.source, '*.md')))
    episodes = []
    for p in files:
        raw = open(p, errors='ignore').read(); fm, body = parse_fm(raw)
        base_name = os.path.basename(p)
        if base_name in ('MEMORY.md', 'memory_ext.md', '_archived_index.md') or base_name in exclude: continue  # индексы — не знания
        mt = manifest.get(base_name, {}).get('mtime_epoch') or os.path.getmtime(p)
        ref = datetime.datetime.fromtimestamp(mt, datetime.timezone.utc)
        head = ''
        if fm.get('historical') == 'true': head = f"[АРХИВ dev, написано {fm.get('written_at','?')}, роли серверов до 2026-07-26] "
        elif fm.get('status'): head = f"[статус на 2026-09-15: {fm['status']}] "
        desc = fm.get('description', '')
        for i, c in enumerate(chunks(body)):
            name = f'{base_name}#{i+1}'
            if a.hash_names:
                import hashlib; name += '@' + hashlib.sha1(c.encode()).hexdigest()[:8]
            if name in done or name in skip: continue
            content = (f'{head}{desc}\n\n' if (i == 0 and desc) else (f'{head}\n\n' if head else '')) + c
            episodes.append(RawEpisode(name=name, content=content, source_description=f'файл памяти {base_name}', source=EpisodeType.text, reference_time=ref))
    if a.offset: episodes = episodes[a.offset:]
    if a.limit: episodes = episodes[:a.limit]
    print(f'[{a.group}] файлов={len(files)} новых эпизодов={len(episodes)} (уже загружено {len(done)}, пропущено из других групп {len(skip)}) модель={model} эмбеддер={emb_model}/{emb_dim} batch={a.batch}', flush=True)
    if a.dry_run or not episodes: return

    llm, embedder, reranker = make_clients(api_key, base, model, emb_model, emb_dim, emb_base)
    # свой клиент с большим пулом: дефолтный пул redis-py упирается в «Too many connections» при SEMAPHORE_LIMIT≥20
    fdb = FalkorDB(host=os.environ.get('FALKORDB_HOST', 'localhost'), port=6379, max_connections=int(os.environ.get('FALKOR_MAX_CONN', '512')))
    driver = FalkorDriver(falkor_db=fdb, database=os.environ.get('FALKORDB_DATABASE', 'default_db'))
    g = Graphiti(graph_driver=driver, llm_client=llm, embedder=embedder, cross_encoder=reranker, max_coroutines=int(os.environ.get('SEMAPHORE_LIMIT', '20')))
    await g.build_indices_and_constraints()
    types = doc_only_types(a.config); etypes = doc_only_edge_types(a.config)
    t_all = time.time()
    for bi in range(0, len(episodes), a.batch):
        batch = episodes[bi:bi + a.batch]; t0 = time.time()
        try:
            await g.add_episode_bulk(batch, group_id=a.group, entity_types=types, edge_types=etypes, custom_extraction_instructions=INSTR + INSTR_COMMON)
            done.update(e.name for e in batch)
            json.dump(sorted(done), open(state_path, 'w'), ensure_ascii=False)
            print(f'  batch {bi//a.batch+1}/{(len(episodes)+a.batch-1)//a.batch}: {len(batch)} эпизодов за {time.time()-t0:.0f}с; всего {len(done)}', flush=True)
        except Exception as e:
            print(f'  !! batch {bi//a.batch+1} ошибка: {str(e)[:300]}', flush=True)
    print(f'[{a.group}] готово за {(time.time()-t_all)/60:.1f} мин', flush=True)
    if falkor_vector_patch: print(f'[{a.group}] vector_patch: {dict(falkor_vector_patch.stats)}', flush=True)
    await g.close()

if __name__ == '__main__':
    asyncio.run(main())
