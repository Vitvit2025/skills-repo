#!/bin/bash
# Развернуть графовую память на чистом сервере по graphiti-memory.yaml.
#   scripts/setup.sh --config my.graphiti-memory.yaml [--firewall] [--mcp] [--tei] [--cron]
# Что делает: deploy_dir (conf/ scripts/ state/ sessions_filtered/), .env из .env.example (если нет), docker-compose.yml
# по конфигу, config.yaml MCP, host-venv (redis, pyyaml), `docker compose up -d`, проверка MCP;
#   --firewall: правило ip6tables-DROP на порты (IPv6 обходит привязку к 127.0.0.1);  --mcp: `claude mcp add`;
#   --tei: локальный эмбеддер (tei.sh);  --cron: строка крона ночной докачки.
# Идемпотентно: повторный запуск обновляет scripts/ и compose, .env и conf не перезаписывает.
set -e
SK=$(cd "$(dirname "$0")" && pwd); CFG=""; FW=0; MCP=0; TEI=0; CRON=0
while [ $# -gt 0 ]; do case "$1" in --config) CFG=$2; shift;; --firewall) FW=1;; --mcp) MCP=1;; --tei) TEI=1;; --cron) CRON=1;; *) echo "неизвестный аргумент $1"; exit 1;; esac; shift; done
[ -f "$CFG" ] || { echo "нужен --config <graphiti-memory.yaml> (пример: examples/prod-ams.graphiti-memory.yaml)"; exit 1; }
python3 -c "import yaml" 2>/dev/null || { echo "нужен pyyaml: apt install -y python3-yaml"; exit 1; }
DEPLOY=$(GRAPHITI_MEMORY_CONFIG=$CFG python3 "$SK/gm_config.py" get deploy_dir)
echo "== deploy_dir: $DEPLOY"
mkdir -p "$DEPLOY"/{conf,scripts,state,sessions_filtered}; chmod 700 "$DEPLOY/sessions_filtered"
[ -f "$DEPLOY/conf/graphiti-memory.yaml" ] || cp "$CFG" "$DEPLOY/conf/graphiti-memory.yaml"; chmod 600 "$DEPLOY/conf/graphiti-memory.yaml"
cp "$SK"/*.py "$SK"/*.sh "$DEPLOY/scripts/"; chmod 700 "$DEPLOY"/scripts/*.sh
[ -f "$DEPLOY/config.yaml" ] || cp "$SK/config.yaml" "$DEPLOY/config.yaml"
if [ ! -f "$DEPLOY/.env" ]; then cp "$SK/.env.example" "$DEPLOY/.env"; chmod 600 "$DEPLOY/.env"; echo "!! заполни $DEPLOY/.env (OPENAI_API_KEY) и перезапусти setup.sh"; fi
chmod 600 "$DEPLOY/.env"
export GRAPHITI_MEMORY_CONFIG="$DEPLOY/conf/graphiti-memory.yaml"
python3 "$SK/gm_config.py" render-compose > "$DEPLOY/docker-compose.yml"
python3 "$SK/gm_config.py" check || echo "!! проверь конфиг (см. ❌ выше)"
# host venv для scrub_graph (redis) — если host_python указывает в venv, которого нет
HP=$(python3 "$SK/gm_config.py" get host_python)
if [[ "$HP" == */.venv/bin/python ]] && [ ! -x "$HP" ]; then python3 -m venv "$(dirname "$(dirname "$HP")")" && "$(dirname "$HP")/pip" install -q redis pyyaml && echo "== venv $HP: redis, pyyaml"; fi
cd "$DEPLOY" && docker compose up -d && echo "== контейнер поднят; ждём healthy…" && sleep 20
PORT=$(python3 "$SK/gm_config.py" get ports.mcp)
curl -s -o /dev/null -w "MCP http://127.0.0.1:$PORT/mcp → HTTP %{http_code} (406/400 = жив, ждёт MCP-заголовки)\n" "http://127.0.0.1:$PORT/mcp" || true
if [ $FW = 1 ]; then
  P="$(python3 "$SK/gm_config.py" get ports.falkordb),$(python3 "$SK/gm_config.py" get ports.ui),$PORT"
  IF=$(ip route | awk '/default/ {print $5; exit}')
  ip6tables -C INPUT -i "$IF" -p tcp -m multiport --dports "$P" -j DROP 2>/dev/null || ip6tables -I INPUT -i "$IF" -p tcp -m multiport --dports "$P" -j DROP
  echo "== ip6tables: DROP $P на $IF (сохрани: netfilter-persistent save / ip6tables-save > /etc/iptables/rules.v6)"
fi
if [ $MCP = 1 ]; then command -v claude >/dev/null && claude mcp add --transport http --scope user graphiti "http://127.0.0.1:$PORT/mcp" || echo "claude CLI не найден — зарегистрируй MCP вручную"; fi
if [ $TEI = 1 ]; then "$DEPLOY/scripts/tei.sh"; fi
if [ $CRON = 1 ]; then
  SCHED=$(python3 "$SK/gm_config.py" get cron.schedule); LINE="$SCHED $DEPLOY/scripts/cron_load.sh >/dev/null 2>>$DEPLOY/cron_load.err # GRAPHITI_NIGHTLY_LOAD"
  (crontab -l 2>/dev/null | grep -v GRAPHITI_NIGHTLY_LOAD; echo "$LINE") | crontab - && echo "== крон: $LINE"
fi
echo "== готово. Дальше: scripts/status.sh; первая загрузка — nohup setsid $DEPLOY/scripts/build_main.sh > $DEPLOY/build_main.log 2>&1 &"
