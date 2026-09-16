#!/usr/bin/env python3
"""Загрузка ГОТОВЫХ эпизодов из JSONL (транскрипты сессий после secret_filter) в Graphiti через add_episode_bulk.
Запуск ВНУТРИ контейнера graphiti-mcp (те же env, что у bulk_load.py):
  docker exec $ENV graphiti-mcp /app/mcp/.venv/bin/python /app/tools/bulk_load_sessions.py \
      --episodes /data/sessions_filtered/episodes.jsonl --group sess_2026-06 [--batch 12] [--limit N] [--dry-run]
Формат строки JSONL: {"name","group","content","reference_time"(ISO),"source_description"}.
Грузятся только эпизоды с group == --group. Идемпотентно: state/<group>.json (как у bulk_load.py).
"""
import argparse, asyncio, datetime, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import embed_chunk_patch  # noqa: F401  — локальный TEI принимает ≤32 текстов за запрос
if os.environ.get('VECTOR_INDEX', '0') == '1':
    import falkor_vector_patch  # дедуп через векторный индекс FalkorDB вместо полного скана
else:
    falkor_vector_patch = None
from bulk_load import INSTR, INSTR_COMMON, doc_only_types, doc_only_edge_types, make_clients
from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver
from falkordb.asyncio import FalkorDB
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.nodes import EpisodeType
from graphiti_core.utils.bulk_utils import RawEpisode

INSTR_SESS = ("Текст — ТРАНСКРИПТ диалога владельца (русский, на «ты») с ассистентом Claude на dev-сервере, до переезда 2026-07-26. "
    "Извлекай: принятые решения, факты об инфраструктуре и проектах, проблемы и их причины, правила и предпочтения владельца. "
    "Не извлекай сущности из приветствий, служебных фраз, промежуточных рассуждений, путей к файлам, имён файлов, кодов ошибок, "
    "переменных окружения, сетевых интерфейсов, портов; скрипты и ранбуки с именем — можно. "
    "IP-адрес, hostname и прозвище одной машины — ОДНА сущность Server, называй её по IP. "
    "Ассистента (Claude, Claude Code) НЕ делай субъектом фактов: не «Claude Code установил X», а «X установлен на 45.145.168.13»; "
    "владелец — сущность Human «Владелец» (одна, без дублей по имени). "
    "Метки [REDACTED:…] — вырезанные секреты: это не сущности и не значения; значения секретов никогда не извлекай, только названия и где лежат. "
    "Даты вида 26.07 или 14.09 относятся к 2026 году. invalid_at ставь ТОЛЬКО если в тексте явно сказано, что факт перестал быть верным "
    "(«закрыто», «больше не», «устарело», «до <дата>», «заменили на»); не выдумывай даты окончания.")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--episodes', required=True); ap.add_argument('--group', required=True)
    ap.add_argument('--batch', type=int, default=12); ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--config', default='/app/mcp/config/config.yaml'); ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--any-group', action='store_true', help='грузить ВСЕ эпизоды файла в --group, игнорируя поле group (единый граф main)')
    ap.add_argument('--instr', default='', help='подменить инструкцию извлечения (по умолчанию INSTR_SESS)')
    a = ap.parse_args()
    instr = (a.instr or INSTR_SESS) + INSTR_COMMON
    api_key = os.environ['OPENAI_API_KEY']; base = os.environ.get('OPENAI_API_URL', 'http://172.17.0.1:18004/v1')
    model = os.environ.get('MODEL_NAME', 'google/gemini-2.5-flash-lite'); emb_model = os.environ.get('EMBEDDER_MODEL', 'BAAI/bge-m3')
    emb_dim = int(os.environ.get('EMBEDDER_DIMENSIONS', '1024')); emb_base = os.environ.get('EMBEDDER_API_URL', 'http://172.17.0.1:18081/v1')
    state_dir = '/app/tools/state'; os.makedirs(state_dir, exist_ok=True)
    state_path = f'{state_dir}/{a.group}.json'
    import fcntl  # одна группа — один загрузчик: второй экземпляр выходит сразу (иначе дубли за батч)
    lock = open(f'{state_dir}/{a.group}.lock', 'w')
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: print(f'[{a.group}] уже грузится другим процессом — выхожу', flush=True); return
    done = set(json.load(open(state_path))) if os.path.exists(state_path) else set()

    episodes = []
    for line in open(a.episodes):
        e = json.loads(line)
        if (not a.any_group and e['group'] != a.group) or e['name'] in done: continue
        ts = e['reference_time'].replace('Z', '+00:00')
        ref = datetime.datetime.fromisoformat(ts) if ts else datetime.datetime.now(datetime.timezone.utc)
        episodes.append(RawEpisode(name=e['name'], content=e['content'], source_description=e['source_description'],
                                   source=EpisodeType.text, reference_time=ref))
    if a.limit: episodes = episodes[:a.limit]
    print(f'[{a.group}] новых эпизодов={len(episodes)} (уже загружено {len(done)}) модель={model} эмбеддер={emb_model}/{emb_dim} batch={a.batch}', flush=True)
    if a.dry_run or not episodes: return

    llm, embedder, reranker = make_clients(api_key, base, model, emb_model, emb_dim, emb_base)
    fdb = FalkorDB(host=os.environ.get('FALKORDB_HOST', 'localhost'), port=6379, max_connections=int(os.environ.get('FALKOR_MAX_CONN', '512')))
    driver = FalkorDriver(falkor_db=fdb, database=os.environ.get('FALKORDB_DATABASE', 'default_db'))
    g = Graphiti(graph_driver=driver, llm_client=llm, embedder=embedder, cross_encoder=reranker, max_coroutines=int(os.environ.get('SEMAPHORE_LIMIT', '20')))
    await g.build_indices_and_constraints()
    types = doc_only_types(a.config); etypes = doc_only_edge_types(a.config)
    t_all = time.time(); n_err = 0
    for bi in range(0, len(episodes), a.batch):
        batch = episodes[bi:bi + a.batch]; t0 = time.time()
        try:
            await g.add_episode_bulk(batch, group_id=a.group, entity_types=types, edge_types=etypes, custom_extraction_instructions=instr)
            done.update(e.name for e in batch)
            json.dump(sorted(done), open(state_path, 'w'), ensure_ascii=False)
            print(f'  batch {bi//a.batch+1}/{(len(episodes)+a.batch-1)//a.batch}: {len(batch)} эпизодов за {time.time()-t0:.0f}с; всего {len(done)}', flush=True)
        except Exception as e:
            n_err += 1; print(f'  !! batch {bi//a.batch+1} ошибка: {str(e)[:300]}', flush=True)
    print(f'[{a.group}] готово за {(time.time()-t_all)/60:.1f} мин, батчей с ошибкой: {n_err}', flush=True)
    if falkor_vector_patch: print(f'[{a.group}] vector_patch: {dict(falkor_vector_patch.stats)}', flush=True)
    await g.close()

if __name__ == '__main__':
    asyncio.run(main())
