#!/bin/bash
# Снимок FalkorDB ПЕРЕД любым разрушительным действием (GRAPH.DELETE, clear_graph, пересоздание контейнера, смена эмбеддера):
#   scripts/backup.sh [метка]      → <backup.dir>/dump-<дата>-<метка>.rdb (600), ретенция backup.keep (по умолчанию 5)
# Правило владельца: сначала снимок, потом действие; путь снимка и команду отката — в отчёт.
# Механика: redis-cli SAVE (синхронно, секунды) → копия dump.rdb из тома контейнера. Откат: остановить контейнер,
# положить файл как dump.rdb в том, поднять (RDB 360 МБ грузится ~1 мин; контейнер с нашим start-services.sh ждёт PONG).
. "$(dirname "$0")/lib.sh"
LABEL=${1:-manual}; DIR=$GM_BACKUP_DIR; KEEP=${GM_BACKUP_KEEP:-5}
mkdir -p "$DIR"; chmod 700 "$DIR"
VOL=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/lib/falkordb/data"}}{{.Source}}{{end}}{{end}}' "$GM_CONTAINER" 2>/dev/null)
[ -n "$VOL" ] && [ -d "$VOL" ] || { echo "!! том FalkorDB контейнера $GM_CONTAINER не найден"; exit 1; }
docker exec "$GM_CONTAINER" redis-cli SAVE >/dev/null || { echo "!! redis-cli SAVE не прошёл (база грузится? занята?)"; exit 1; }
OUT="$DIR/dump-$(date -u +%F-%H%M)-$LABEL.rdb"
cp -p "$VOL/dump.rdb" "$OUT" && chmod 600 "$OUT" || { echo "!! копия не удалась"; exit 1; }
echo "бэкап: $OUT ($(du -h "$OUT" | cut -f1)); откат: docker stop $GM_CONTAINER && cp $OUT $VOL/dump.rdb && docker start $GM_CONTAINER"
# ретенция: оставить KEEP последних
ls -1t "$DIR"/dump-*.rdb 2>/dev/null | tail -n +$((KEEP+1)) | while read -r f; do rm -f "$f"; echo "удалён старый снимок: $f"; done
