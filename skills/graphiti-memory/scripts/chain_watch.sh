#!/bin/bash
# Сторож долгой загрузки: каждые 2 мин проверка, каждые 20 мин сводка. Печатает строки-события (для Monitor / tmux / tail):
#   PROGRESS … — сводка;  ALERT … — цепочка умерла / застой >10 мин / новая ошибка порции / Traceback;  DONE — завершено.
# Параметры через env: LOG (лог цепочки, по умолчанию <deploy_dir>/build_main.log), PROC (шаблон pgrep процесса цепочки),
#   DONE_MARK (строка завершения, по умолчанию «цепочка завершена»; для fixup — «добивка завершена»).
# Грабли (15.09.2026): лог с выводом модели «бинарный» для grep → везде `grep -a`; переменную со списком state НЕ называть
# GROUPS (встроенный массив bash); счётчики ошибок стартуют с текущего значения в логе — иначе перезапущенный сторож
# повторно алертит про старую ошибку.
. "$(dirname "$0")/lib.sh"
LOG=${LOG:-$GM_DEPLOY_DIR/build_main.log}; PROC=${PROC:-build_main.sh}; DONE_MARK=${DONE_MARK:-цепочка завершена}; STATE=$GM_STATE_DIR/$GM_GRAPH.json
last_err=$(grep -a -c "!! batch" "$LOG" 2>/dev/null || echo 0); last_tb=$(grep -a -c "Traceback" "$LOG" 2>/dev/null || echo 0)
tick=0; last_progress_line=""; stall_since=$(date +%s)
[ "$last_err" != "0" ] && echo "INFO $(date -u +%H:%M) в логе уже $last_err сорванных порций (алерты — только о новых; добивка: scripts/fixup.sh)"
summary() {
  local stage; stage=$(grep -a -E "^===" "$LOG" 2>/dev/null | grep -a -v "склейка" | tail -1 | cut -c5-)
  local err; err=$(grep -a -c "!! batch" "$LOG" 2>/dev/null)
  local bt; bt=$(grep -a -oE "за [0-9]+с" "$LOG" 2>/dev/null | tail -10 | grep -oE "[0-9]+" | awk '{s+=$1;n++} END{if(n) printf "%d", s/n}')
  local cur; cur=$(grep -a -E "batch [0-9]+/[0-9]+" "$LOG" 2>/dev/null | tail -1 | grep -oE "batch [0-9]+/[0-9]+")
  local st; st=$(python3 -c "import json;print(len(json.load(open('$STATE'))))" 2>/dev/null)
  echo "PROGRESS $(date -u +%H:%M) | этап: $stage | порция: ${cur:-?} | state: ${st:-0} | ср. порция (последние 10): ${bt:-?} с | ошибок порций: $err | load: $(cut -d' ' -f1 /proc/loadavg)"
}
while true; do
  if grep -a -q "$DONE_MARK" "$LOG" 2>/dev/null; then echo "DONE $(date -u +%H:%M) $DONE_MARK. $(summary)"; exit 0; fi
  if ! pgrep -f "$PROC" >/dev/null; then echo "ALERT $(date -u +%H:%M) $PROC не запущен, а лог без '$DONE_MARK'. Хвост: $(tail -2 "$LOG" | tr '\n' ' ' | cut -c1-300)"; sleep 120; continue; fi
  err=$(grep -a -c "!! batch" "$LOG" 2>/dev/null); if [ "$err" -gt "$last_err" ]; then echo "ALERT $(date -u +%H:%M) новая ошибка порции: $(grep -a "!! batch" "$LOG" | tail -1 | cut -c1-250)"; last_err=$err; fi
  tb=$(grep -a -c "Traceback" "$LOG" 2>/dev/null); if [ "$tb" -gt "$last_tb" ]; then echo "ALERT $(date -u +%H:%M) Traceback: $(grep -a -A3 Traceback "$LOG" | tail -4 | tr '\n' ' ' | cut -c1-300)"; last_tb=$tb; fi
  cur=$(grep -a -E "batch [0-9]+/|^===" "$LOG" 2>/dev/null | tail -1)
  if [ "$cur" != "$last_progress_line" ]; then last_progress_line="$cur"; stall_since=$(date +%s); fi
  if [ $(( $(date +%s) - stall_since )) -gt 600 ]; then echo "ALERT $(date -u +%H:%M) застой: нет новых порций >10 мин. Последнее: $cur"; stall_since=$(date +%s); fi
  tick=$((tick+1)); if [ $((tick % 10)) -eq 0 ]; then summary; fi
  sleep 120
done
