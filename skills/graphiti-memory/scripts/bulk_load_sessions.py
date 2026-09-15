#!/usr/bin/env python3
"""Загрузка ГОТОВЫХ эпизодов из JSONL (транскрипты сессий / inbox после secret_filter) в Graphiti через add_episode_bulk.
Запуск ВНУТРИ контейнера (те же env, что у bulk_load.py):
  docker exec $GM_DOCKER_ENV -e OPENAI_API_KEY=… graphiti-mcp /app/mcp/.venv/bin/python /app/loaders/bulk_load_sessions.py \
      --episodes /data/sessions_filtered/episodes_transcripts.jsonl --group main --any-group --instr transcripts [--batch 12]
Формат строки JSONL: {"name","group","content","reference_time"(ISO),"source_description"}.
Без --any-group грузятся только эпизоды с group == --group (старое поведение); с ним — все эпизоды файла (единый граф).
--instr: ключ instructions.* из graphiti-memory.yaml или готовый текст. Идемпотентно: state/<group>.json, flock на группу.
"""
import argparse, asyncio, datetime, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gm_config import cfg
import embed_chunk_patch  # noqa: F401
if os.environ.get('VECTOR_INDEX', '0') == '1':
    import falkor_vector_patch
else:
    falkor_vector_patch = None
from bulk_load import doc_only_types, open_graphiti, take_lock, env_or_cfg
from graphiti_core.nodes import EpisodeType
from graphiti_core.utils.bulk_utils import RawEpisode


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--episodes', required=True); ap.add_argument('--group', default=None)
    ap.add_argument('--batch', type=int, default=None); ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--mcp-config', default='/app/mcp/config/config.yaml'); ap.add_argument('--config', default=None)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--any-group', action='store_true', help='грузить ВСЕ эпизоды файла в --group, игнорируя поле group (единый граф)')
    ap.add_argument('--instr', default='transcripts', help='ключ instructions.* или текст инструкции извлечения')
    a = ap.parse_args()
    c = cfg(a.config)
    group = a.group or c.graph; batch = a.batch or int(c.get('load.batch', 12))
    instr = c.instruction(a.instr) if a.instr in (c.get('instructions') or {}) else a.instr
    api_key = os.environ['OPENAI_API_KEY']; base = env_or_cfg(c, 'OPENAI_API_URL', 'llm.api_url', 'https://openrouter.ai/api/v1')
    model = env_or_cfg(c, 'MODEL_NAME', 'llm.model'); emb_model = env_or_cfg(c, 'EMBEDDER_MODEL', 'embedder.model')
    emb_dim = int(env_or_cfg(c, 'EMBEDDER_DIMENSIONS', 'embedder.dimensions', 1024))
    emb_base = env_or_cfg(c, 'EMBEDDER_API_URL', 'embedder.bulk_api_url', base)
    if not os.environ.get('SMALL_MODEL_NAME') and c.get('llm.small_model'): os.environ['SMALL_MODEL_NAME'] = c.get('llm.small_model')
    state_dir = c.state_dir(); os.makedirs(state_dir, exist_ok=True)
    state_path = f'{state_dir}/{group}.json'
    lock = take_lock(state_dir, group)
    if not lock: return
    done = set(json.load(open(state_path))) if os.path.exists(state_path) else set()

    episodes = []
    for line in open(a.episodes):
        e = json.loads(line)
        if (not a.any_group and e['group'] != group) or e['name'] in done: continue
        ts = (e.get('reference_time') or '').replace('Z', '+00:00')
        ref = datetime.datetime.fromisoformat(ts) if ts else datetime.datetime.now(datetime.timezone.utc)
        if ref.tzinfo is None: ref = ref.replace(tzinfo=datetime.timezone.utc)
        episodes.append(RawEpisode(name=e['name'], content=e['content'], source_description=e.get('source_description', ''),
                                   source=EpisodeType.text, reference_time=ref))
    if a.limit: episodes = episodes[:a.limit]
    print(f'[{group}] файл={a.episodes} новых эпизодов={len(episodes)} (уже загружено {len(done)}) модель={model} эмбеддер={emb_model}/{emb_dim} batch={batch}', flush=True)
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
