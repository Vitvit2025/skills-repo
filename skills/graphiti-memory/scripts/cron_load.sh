#!/bin/bash
# Ночная докачка в единый граф (крон). Идемпотентно: имена эпизодов с хешем содержимого — новые/изменённые куски
# грузятся, остальные пропускаются (state/<graph>.json).
#   1) память этого сервера (--hash-names) → graph;  2) транскрипты (idle ≥ N ч, secret_filter) → graph;  3) inbox → graph
#   4) склейка псевдонимов (merge_aliases);  5) контроль секретов (scrub_graph, ожидается 0)
#   6) сводка → cron_load.log; Telegram владельцу — ТОЛЬКО при ошибках/секретах (alerts.env_file)
# Крон (cron.schedule из конфига): 30 3 * * * <deploy_dir>/scripts/cron_load.sh >/dev/null 2>><deploy_dir>/cron_load.err
set -u
. "$(dirname "$0")/lib.sh"
LOCK=$(python3 "$GM_SCRIPTS/gm_config.py" get cron.lock); LOCK=${LOCK:-/run/graphiti-cron.lock}
exec 9>"$LOCK"; flock -n 9 || { echo "$(date -u +%FT%TZ) уже идёт, выходим"; exit 0; }
LOG=$GM_DEPLOY_DIR/cron_load.log; T0=$(date +%s); G=$GM_GRAPH
log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$LOG"; }
SUM=""; ERR=0
run_load() {  # tag, loader args...
  local tag=$1; shift
  local out; out=$($DEX "$@" --group "$G" --batch "$GM_BATCH" 2>&1 | gm_filt_raw)   # строки `!! batch` фильтр не глотает
  echo "$out" >> "$LOG"
  local n; n=$(echo "$out" | grep -a -oE "новых эпизодов=[0-9]+" | grep -oE "[0-9]+" | head -1)
  local e; e=$(echo "$out" | grep -a -c "!! batch")
  ERR=$((ERR+e)); SUM="$SUM $tag:+${n:-?}${e:+/ошибок $e}"
}
log "=== старт докачки → $G"
gm_sync_loaders
# 1) память
run_load memory /app/loaders/bulk_load.py --source memory --hash-names --instr memory
# 2) транскрипты этого сервера
if [ -n "$GM_HAS_TRANSCRIPTS" ]; then
  "$GM_HOST_PY" "$GM_SCRIPTS/sessions_extract.py" --source transcripts >> "$LOG" 2>&1 \
    && "$GM_HOST_PY" "$GM_SCRIPTS/sessions_extract.py" --source transcripts --check >> "$LOG" 2>&1 \
    && gm_push "$GM_WORK_DIR/episodes_transcripts.jsonl" \
    && run_load transcripts /app/loaders/bulk_load_sessions.py --episodes /data/sessions_filtered/episodes_transcripts.jsonl --any-group --instr transcripts \
    || { log "!! транскрипты: экстракция/проверка не прошла"; ERR=$((ERR+1)); }
fi
# 3) inbox
if [ -n "$GM_HAS_INBOX" ]; then
  "$GM_HOST_PY" "$GM_SCRIPTS/inbox_extract.py" >> "$LOG" 2>&1 \
    && gm_push "$GM_WORK_DIR/episodes_inbox.jsonl" \
    && run_load inbox /app/loaders/bulk_load_sessions.py --episodes /data/sessions_filtered/episodes_inbox.jsonl --any-group --instr inbox \
    || { log "!! inbox: экстракция не прошла"; ERR=$((ERR+1)); }
fi
# 4) склейка
$DEX /app/loaders/merge_aliases.py --graph "$G" --apply 2>&1 | grep -a -E "готово" >> "$LOG"
# 5) контроль секретов
SCRUB=$(gm_scrub | grep -a -oE "полей с секретами/телефонами: [0-9]+" | awk '{s+=$NF} END{print s+0}')
[ "${SCRUB:-0}" != "0" ] && log "!! контроль секретов: исправлено $SCRUB полей (докачка пропустила секреты — проверить фильтр)"
MSG="Graphiti-докачка $(date -u +%d.%m\ %H:%M) → $G:$SUM; секретов после фильтра: ${SCRUB:-0}; ошибок: $ERR$([ "$ERR" != 0 ] && echo ' (сорванные порции догрузит следующий прогон или scripts/fixup.sh)'); $(( ($(date +%s)-T0)/60 )) мин; $(gm_counts)"
log "$MSG"
# 6) Telegram — только при ошибках/секретах
if [ "$ERR" != "0" ] || [ "${SCRUB:-0}" != "0" ]; then gm_alert "⚠️ $MSG"; fi
