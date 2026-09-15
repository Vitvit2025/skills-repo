#!/bin/bash
# Сборка ЕДИНОГО графа (graph из graphiti-memory.yaml, обычно `main`) из всех источников по build.steps:
#   archive <label>            — архив памяти (*.md, mtime из манифеста) через bulk_load.py
#   archive_transcripts <label>— старые транскрипты: sessions_extract (фильтр секретов) → --check → bulk_load_sessions --any-group
#   memory                     — файловая память этого сервера (--hash-names: изменённый кусок грузится заново)
#   transcripts                — транскрипты этого сервера (idle ≥ N ч)
#   inbox                      — Telegram-инбокс
# После КАЖДОГО шага — merge_aliases (склейка псевдонимов); в конце — scrub_graph (контроль секретов, ожидается 0).
# Один писатель на граф (flock в загрузчиках) — параллелить НЕЛЬЗЯ. Идемпотентно: state/<graph>.json — перезапуск продолжает.
# Загрузчик, умерший без «готово», перезапускается до 3 раз. Порядок: самый большой блок первым, дальше хронологически.
# Запуск: nohup setsid scripts/build_main.sh > build_main.log 2>&1 &     Сторож: scripts/chain_watch.sh (Monitor / tmux).
# Совет: если самый большой архив уже загружен в отдельный граф, вместо шага archive скопируй его:
#   redis-cli GRAPH.COPY <старый> main; MATCH (n) SET n.group_id='main'; MATCH ()-[r]->() SET r.group_id='main'; cp state/<старый>.json state/main.json
. "$(dirname "$0")/lib.sh"
LOG=${LOG:-$GM_DEPLOY_DIR/build_main.log}; G=$GM_GRAPH; BATCH=${BATCH:-$GM_BATCH}
load() { # tag, loader args...  (с автоперезапуском по логу)
  local tag=$1; shift; local n
  for n in 1 2 3; do
    $DEX "$@" --group "$G" --batch "$BATCH" 2>&1 | gm_filt "$tag"
    if grep -q "^\[$tag\] \[$G\] готово" "$LOG" 2>/dev/null || grep -q "^\[$tag\] \[$G\] .*новых эпизодов=0" "$LOG" 2>/dev/null; then return 0; fi
    gm_log "!! $tag: загрузчик завершился без «готово» (попытка $n) — перезапуск через 60 с"; sleep 60
  done
  gm_log "!! $tag: 3 попытки без «готово», иду дальше"; return 1
}
extract_load() { # tag, source-key, instr, extra sessions_extract args...
  local tag=$1 key=$2 instr=$3; shift 3
  local f="$GM_WORK_DIR/episodes_$key.jsonl"
  if "$GM_HOST_PY" "$GM_SCRIPTS/sessions_extract.py" --source "$key" "$@" 2>&1 | tail -3 \
     && "$GM_HOST_PY" "$GM_SCRIPTS/sessions_extract.py" --source "$key" --check 2>&1 | tail -1 | tee /dev/stderr | grep -q "утечек: нет"; then
    gm_push "$f"
    load "$tag" /app/loaders/bulk_load_sessions.py --episodes "/data/sessions_filtered/episodes_$key.jsonl" --any-group --instr "$instr"
  else
    gm_log "!! $tag: экстракция/проверка не прошла — шаг пропущен"; ERR=$((ERR+1))
  fi
}

ERR=0
gm_log "старт сборки $G (потоков=$GM_SEMAPHORE, порция=$BATCH, конфиг $GM_CONFIG)"
gm_sync_loaders
while read -r kind key instr; do
  [ -n "$kind" ] || continue
  case "$kind" in
    archive)             gm_log "шаг: архив памяти $key → $G"; load "$key" /app/loaders/bulk_load.py --source "$key" --instr "$instr" ;;
    archive_transcripts) gm_log "шаг: старые транскрипты $key → $G"; extract_load "$key" "$key" "$instr" ;;
    memory)              gm_log "шаг: память этого сервера → $G"; load memory /app/loaders/bulk_load.py --source memory --hash-names --instr "$instr" ;;
    transcripts)         gm_log "шаг: транскрипты этого сервера → $G (idle ≥ $GM_MIN_IDLE_HOURS ч)"; extract_load transcripts transcripts "$instr" ;;
    inbox)               gm_log "шаг: inbox → $G"
                         if "$GM_HOST_PY" "$GM_SCRIPTS/inbox_extract.py" 2>&1 | tail -1; then gm_push "$GM_WORK_DIR/episodes_inbox.jsonl"
                           load inbox /app/loaders/bulk_load_sessions.py --episodes /data/sessions_filtered/episodes_inbox.jsonl --any-group --instr "$instr"
                         else gm_log "!! inbox_extract не прошёл — пропущен"; fi ;;
    *) gm_log "!! неизвестный шаг: $kind $key" ;;
  esac
  gm_merge "после $kind $key"
done < <(python3 "$GM_SCRIPTS/gm_config.py" steps)
gm_log "контроль секретов (scrub_graph --graphs $G --apply)"
gm_scrub | tail -15 | sed -u 's/^/[scrub] /'
gm_log "итог $G: $(gm_counts); ошибок порций: $(grep -c '!! batch' "$LOG" 2>/dev/null)"
gm_log "цепочка завершена"
