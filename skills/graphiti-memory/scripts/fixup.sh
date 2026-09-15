#!/bin/bash
# Добивка сорванных порций после build_main.sh / cron_load.sh: повторный прогон загрузчиков по ТЕМ ЖЕ файлам эпизодов.
# State идемпотентен (state/<graph>.json) — грузятся только эпизоды, которых там нет (= сорванные порции), остальное пропускается.
# Порция меньше (6): сорванная порция обычно «тяжёлая» (ответ дедупа рвался). Затем склейка псевдонимов и контроль секретов.
#   scripts/fixup.sh            (все шаги из build.steps; экстракция транскриптов НЕ повторяется — берутся готовые episodes_*.jsonl)
# Когда: после сборки/докачки `grep -a -c "!! batch" build_main.log` > 0. Запуск: nohup setsid scripts/fixup.sh > fixup.log 2>&1 &
. "$(dirname "$0")/lib.sh"
G=$GM_GRAPH; BATCH=${BATCH:-6}; LOG=${LOG:-$GM_DEPLOY_DIR/fixup.log}
run() { local tag=$1; shift; $DEX "$@" --group "$G" --batch "$BATCH" 2>&1 | gm_filt "$tag"; }
gm_log "добивка $G (порция $BATCH)"
gm_sync_loaders
while read -r kind key instr; do
  [ -n "$kind" ] || continue
  case "$kind" in
    archive)             run "$key" /app/loaders/bulk_load.py --source "$key" --instr "$instr" ;;
    memory)              run memory /app/loaders/bulk_load.py --source memory --hash-names --instr "$instr" ;;
    archive_transcripts|transcripts|inbox)
      f="$GM_WORK_DIR/episodes_$key.jsonl"
      if [ -f "$f" ]; then gm_push "$f"; run "$key" /app/loaders/bulk_load_sessions.py --episodes "/data/sessions_filtered/episodes_$key.jsonl" --any-group --instr "$instr"
      else gm_log "!! $kind $key: нет $f (сначала build_main/cron_load) — пропущен"; fi ;;
    *) gm_log "!! неизвестный шаг: $kind $key" ;;
  esac
done < <(python3 "$GM_SCRIPTS/gm_config.py" steps)
gm_merge "после добивки"
gm_log "контроль секретов"; gm_scrub | tail -4 | sed -u 's/^/[scrub] /'
gm_log "итог $G: $(gm_counts); сорванных порций в этом прогоне: $(gm_errs "$LOG") (если >0 — повторить fixup ещё раз)"
gm_log "добивка завершена"
