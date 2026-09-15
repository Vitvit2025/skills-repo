#!/bin/bash
# Сторож долгой загрузки: каждые 2 мин проверка, каждые 20 мин сводка. Печатает строки-события (для Monitor / tmux / tail):
#   PROGRESS … — сводка;  ALERT … — цепочка умерла / застой >10 мин / новая ошибка порции / Traceback;  DONE — завершено.
# Параметры через env: LOG (лог цепочки, по умолчанию <deploy_dir>/build_main.log), PROC (шаблон pgrep процесса цепочки).
. "$(dirname "$0")/lib.sh"
LOG=${LOG:-$GM_DEPLOY_DIR/build_main.log}; PROC=${PROC:-build_main.sh}; STATE=$GM_STATE_DIR/$GM_GRAPH.json
last_err=0; last_tb=0; tick=0; last_progress_line=""; stall_since=$(date +%s)
summary() {
  local stage; stage=$(grep -E "^===" "$LOG" 2>/dev/null | grep -v "склейка" | tail -1 | cut -c5-)
  local err; err=$(grep -c "!! batch" "$LOG" 2>/dev/null)
  local bt; bt=$(grep -oE "за [0-9]+с" "$LOG" 2>/dev/null | tail -10 | grep -oE "[0-9]+" | awk '{s+=$1;n++} END{if(n) printf "%d", s/n}')
  local cur; cur=$(grep -E "batch [0-9]+/[0-9]+" "$LOG" 2>/dev/null | tail -1 | grep -oE "batch [0-9]+/[0-9]+")
  local st; st=$(python3 -c "import json;print(len(json.load(open('$STATE'))))" 2>/dev/null)
  echo "PROGRESS $(date -u +%H:%M) | этап: $stage | порция: ${cur:-?} | state: ${st:-0} | ср. порция (последние 10): ${bt:-?} с | ошибок порций: $err | load: $(cut -d' ' -f1 /proc/loadavg)"
}
while true; do
  if grep -q "цепочка завершена" "$LOG" 2>/dev/null; then echo "DONE $(date -u +%H:%M) цепочка завершена. $(summary)"; exit 0; fi
  if ! pgrep -f "$PROC" >/dev/null; then echo "ALERT $(date -u +%H:%M) $PROC не запущен, а лог без 'цепочка завершена'. Хвост: $(tail -2 "$LOG" | tr '\n' ' ' | cut -c1-300)"; sleep 120; continue; fi
  err=$(grep -c "!! batch" "$LOG" 2>/dev/null); if [ "$err" -gt "$last_err" ]; then echo "ALERT $(date -u +%H:%M) новая ошибка порции: $(grep "!! batch" "$LOG" | tail -1 | cut -c1-250)"; last_err=$err; fi
  tb=$(grep -c "Traceback" "$LOG" 2>/dev/null); if [ "$tb" -gt "$last_tb" ]; then echo "ALERT $(date -u +%H:%M) Traceback: $(grep -A3 Traceback "$LOG" | tail -4 | tr '\n' ' ' | cut -c1-300)"; last_tb=$tb; fi
  cur=$(grep -E "batch [0-9]+/|^===" "$LOG" 2>/dev/null | tail -1)
  if [ "$cur" != "$last_progress_line" ]; then last_progress_line="$cur"; stall_since=$(date +%s); fi
  if [ $(( $(date +%s) - stall_since )) -gt 600 ]; then echo "ALERT $(date -u +%H:%M) застой: нет новых порций >10 мин. Последнее: $cur"; stall_since=$(date +%s); fi
  tick=$((tick+1)); if [ $((tick % 10)) -eq 0 ]; then summary; fi
  sleep 120
done
