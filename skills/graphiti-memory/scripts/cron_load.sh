#!/bin/bash
# Ночная докачка в Graphiti (крон). Идемпотентно: имена эпизодов с хешем содержимого — новые/изменённые куски грузятся,
# остальные пропускаются (state/main.json). Всё — в ЕДИНЫЙ граф main (с 15.09.2026).
# 16.09.2026 (аудит графа): (а) вместо сырых транскриптов — ДАЙДЖЕСТЫ сессий (session_digest.py, Sonnet 5): решения,
# тупики с причиной, открытые вопросы, правила владельца — 1–3 куска на сессию вместо 30–140; (б) дедуп/противоречия —
# на умной модели (SMALL_MODEL_NAME=anthropic/claude-sonnet-5, решение владельца), промпты дедупа подменены
# (tools/prompt_patch.py); (в) типы связей и общие правила извлечения (config.yaml, INSTR_COMMON); (г) метрики качества
# после прогона (metrics.py) + учёт токенов по моделям (state/usage.jsonl); (д) контроль секретов и в сообществах.
#   1) память прода  /root/.claude/projects/-root/memory/*.md  → main   (--hash-names, без project_graphiti_memory.md)
#   2) дайджесты сессий этого прода ~/.claude/projects/-root/*.jsonl (idle ≥ 2 ч) → main
#   3) inbox-бот (тексты)                                        → main
#   4) склейка дублей (merge_aliases --graph main), 5) контроль секретов (scrub_graph, ожидается 0), 6) метрики
#   7) сводка → cron_load.log + Telegram владельцу (бот inbox) ТОЛЬКО при ошибках/секретах/метриках вне нормы
# Крон: 30 3 * * * /root/graphiti-mcp/cron_load.sh   (лог /root/graphiti-mcp/cron_load.log)
set -u
cd /root/graphiti-mcp
exec 9>/run/graphiti-cron.lock; flock -n 9 || { echo "$(date -u +%FT%TZ) уже идёт, выходим"; exit 0; }
LOG=cron_load.log; T0=$(date +%s); T0_ISO=$(date -u +%FT%TZ); RUN_MARK="=== старт докачки $T0_ISO"
log() { echo "$(date -u +%FT%TZ) $*" | tee -a $LOG; }
set -a; . ./.env; set +a
SMALL=${SMALL_MODEL_NAME:-anthropic/claude-sonnet-5}
BASE="-e OPENAI_API_KEY=$OPENAI_API_KEY -e OPENAI_API_URL=https://openrouter.ai/api/v1 -e MODEL_NAME=google/gemini-2.5-flash-lite -e SMALL_MODEL_NAME=$SMALL -e EMBEDDER_MODEL=BAAI/bge-m3 -e EMBEDDER_DIMENSIONS=1024 -e EMBEDDER_API_URL=https://openrouter.ai/api/v1 -e SEMAPHORE_LIMIT=60 -e VECTOR_INDEX=1 -e FALKOR_MAX_CONN=4096 -e GRAPHITI_PATCH=1 -e PYTHONPATH=/app/loaders"
PY=/app/mcp/.venv/bin/python; C=graphiti-mcp; G=main   # единый граф main (решение владельца 15.09.2026)
INSTR_DIGEST="Текст — ДАЙДЖЕСТ сессии владельца (русский, на «ты») с ассистентом Claude Code на боевом сервере 201.51.23.17 (Timeweb, Амстердам): разделы «Решения и результаты», «Тупики», «Открытые вопросы», «Правила и предпочтения владельца». Извлекай факты об инфраструктуре и проектах, решения, тупики с причиной (как TRIED_AND_FAILED — это верные факты, не invalid_at), открытые вопросы (как план), правила владельца (Rule). Не извлекай сущности из путей к файлам, кодов ошибок, переменных окружения, портов; скрипты и ранбуки с именем — можно. IP-адрес, hostname и прозвище одной машины — ОДНА сущность Server, называй её по IP. Ассистента (Claude, Claude Code) НЕ делай субъектом фактов; владелец — сущность Human «Владелец» (одна). Метки [REDACTED:…] — вырезанные секреты: не сущности и не значения. Даты вида 26.07 или 14.09 относятся к 2026 году. invalid_at ставь ТОЛЬКО при явном «закрыто/больше не/устарело/заменили на/до <дата>»."
INSTR_INBOX="Текст — сообщение владельца в свой Telegram-бот-инбокс (заметки, задачи, ссылки, пересланные файлы). Извлекай факты, решения, задачи и предпочтения владельца; владелец — сущность Human «Владелец». Метки [REDACTED:…] — вырезанные секреты, не сущности. Даты вида 26.07 относятся к 2026 году. invalid_at ставь только при явном «закрыто/больше не/устарело»."
# фильтр служебных строк: строки ошибок порций (!! batch) пропускаем ВСЕГДА (раньше «Unterminated» в тексте ошибки глотал их)
filt() { awk '/^ *!! /{print; next} /not found in nodes|invalid duplicate|Unterminated|unknown entity|Warning|_patch:|подменен/{next} {print}'; }
# файл в контейнер: каталог ./sessions_filtered смонтирован (:ro) — cat не сработает, но файл там уже есть
put_file() { docker exec -i $C sh -c "cat > $2" < "$1" 2>/dev/null || docker exec $C test -s "$2"; }
if docker exec $C sh -c 'test -w /app/loaders' 2>/dev/null; then
  for f in bulk_load.py bulk_load_sessions.py falkor_vector_patch.py embed_chunk_patch.py merge_aliases.py sitecustomize.py prompt_patch.py small_model_patch.py; do docker exec -i $C sh -c "cat > /app/loaders/$f" < tools/$f; done
fi
run_load() {  # tag, loader args...  → печатает "tag:+N[/ошибок E]" (SUM собирается снаружи — функция идёт в подоболочке)
  local g=$1; shift
  local out; out=$(docker exec $BASE -e USAGE_TAG=$g $C $PY "$@" --batch 12 2>&1 | filt)
  echo "$out" | sed "s/^/[$g] /" >> $LOG
  local n=$(echo "$out" | grep -a -oE "новых эпизодов=[0-9]+" | grep -oE "[0-9]+" | head -1)
  local e=$(echo "$out" | grep -a -c "!! batch")
  echo "$g:+${n:-?}$([ "$e" != 0 ] && echo "/ошибок $e")"
}
log "$RUN_MARK (small_model=$SMALL)"
SUM=""
# 1) память прода — через фильтр секретов на хосте (в контейнере нет словаря) → очищенная копия с теми же именами/mtime
if .venv/bin/python memory_clean.py >> $LOG 2>&1; then
  SUM="$SUM $(run_load memory /app/loaders/bulk_load.py --source /data/sessions_filtered/memory_clean --group $G --hash-names --exclude project_graphiti_memory.md)"
else log "!! память: фильтр секретов не отработал, загрузка пропущена"; fi
# 2) дайджесты сессий прода (вместо сырых транскриптов)
if .venv/bin/python session_digest.py --src /root/.claude/projects/-root --out sessions_filtered --name episodes_digest.jsonl --min-idle-hours 2 >> $LOG 2>&1 \
   && put_file sessions_filtered/episodes_digest.jsonl /data/sessions_filtered/episodes_digest.jsonl; then
  SUM="$SUM $(run_load digest /app/loaders/bulk_load_sessions.py --episodes /data/sessions_filtered/episodes_digest.jsonl --group $G --any-group --instr "$INSTR_DIGEST")"
else log "!! дайджесты: генерация/проверка/копия не прошла"; fi
# 3) inbox
if python3 inbox_extract.py --out sessions_filtered >> $LOG 2>&1 \
   && put_file sessions_filtered/episodes_inbox.jsonl /data/sessions_filtered/episodes_inbox.jsonl; then
  SUM="$SUM $(run_load inbox /app/loaders/bulk_load_sessions.py --episodes /data/sessions_filtered/episodes_inbox.jsonl --group $G --any-group --instr "$INSTR_INBOX")"
else log "!! inbox: экстракция/копия не прошла"; fi
# 4) склейка дублей (единый граф — один прогон)
docker exec $C $PY /app/loaders/merge_aliases.py --graph $G --apply 2>&1 | grep -a -E "^\[.*готово" >> $LOG
# 5) контроль секретов (карточки, факты, эпизоды, сообщества)
SCRUB=$(.venv/bin/python scrub_graph.py --graphs $G --apply 2>&1 | grep -a -oE "полей с секретами/телефонами: [0-9]+" | awk '{s+=$NF} END{print s+0}')
[ "${SCRUB:-0}" != "0" ] && log "!! контроль секретов: исправлено $SCRUB полей (докачка пропустила секреты — проверить фильтр)"
# 5b) однодневные окна invalid_at, появившиеся в этом прогоне: из извлечения («попытка не сработала» ≠ факт закончился)
#     и у старых фактов, погашенных пересказом того же (обе даты из одного дня) — снять
.venv/bin/python repair_invalidations.py --graph $G --same-day --expired-since "$T0_ISO" --apply 2>&1 | grep -a -E "^(разбор|восстановлено)" | sed 's/^/[same-day] /' >> $LOG
# 6) метрики качества прогона (код 2 = вне нормы)
METRICS=$(.venv/bin/python metrics.py --since "$T0_ISO" --graph $G 2>&1); MRC=$?
log "$METRICS"
# ошибки этого прогона — по логу (строки «!! » после стартовой метки)
ERR=$(awk -v m="$RUN_MARK" 'index($0,m){f=1;next} f && /!! /{c++} END{print c+0}' $LOG)
MSG="Graphiti-докачка в $G $(date -u +%d.%m\ %H:%M): блоки$SUM; секретов после фильтра: ${SCRUB:-0}; ошибок: $ERR; $(( ($(date +%s)-T0)/60 )) мин; $(echo "$METRICS" | grep -oE '\$[0-9.]+' | head -1)"
log "$MSG"
# 7) Telegram владельцу через inbox-бот (только при ошибках/секретах/метриках вне нормы — иначе тихо; полный лог в cron_load.log)
if [ "$ERR" != "0" ] || [ "${SCRUB:-0}" != "0" ] || [ "$MRC" = "2" ]; then
  set -a; . /etc/claude-inbox/env; set +a
  curl -s -m 20 "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" -d chat_id="${ALERT_CHAT_ID:-$OWNER_ID}" --data-urlencode "text=⚠️ $MSG
$(echo "$METRICS" | grep -oE '⚠️.*' | head -1)" >/dev/null 2>&1 || true
fi
