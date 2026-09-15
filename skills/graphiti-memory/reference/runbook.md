# Runbook graphiti-memory — короткие команды

Все команды из `<deploy_dir>` (конфиг `conf/graphiti-memory.yaml`; иначе `export GRAPHITI_MEMORY_CONFIG=…`).
`R="docker exec graphiti-mcp redis-cli"` — имя контейнера из `container`.

## Статус
```bash
scripts/status.sh                                   # контейнеры, графы, карточки/факты/эпизоды, invalid_at, индексы, крон
scripts/gm_config.py check                          # конфиг: пути, env, права
scripts/mcp_client.py                               # MCP жив? список инструментов
scripts/mcp_client.py get_status '{}'
$R GRAPH.LIST
$R GRAPH.QUERY main "MATCH (n:Entity) RETURN count(n)"
$R GRAPH.QUERY main "MATCH ()-[r:RELATES_TO]->() WHERE r.invalid_at IS NOT NULL RETURN count(r)"
$R GRAPH.QUERY main "CALL db.indexes() YIELD label, types RETURN label, types"   # 2 × VECTOR ожидается
$R GRAPH.SLOWLOG main                               # что тормозит базу
```

## Поиск (из сессии — инструменты MCP; из шелла — mcp_client)
```bash
scripts/mcp_client.py search_memory_facts '{"query":"что зависит от прокси Selectel","max_facts":10}'
scripts/mcp_client.py search_nodes '{"query":"прод","max_nodes":5}'
scripts/mcp_client.py add_memory '{"name":"итог 15.09","episode_body":"Решили: …","source":"text","source_description":"итог сессии"}'
```
Cypher по карточке: `$R GRAPH.QUERY main "MATCH (n:Entity) WHERE n.name CONTAINS '201.51' RETURN n.name, labels(n), left(n.summary,200)"`

## Докачка
```bash
scripts/cron_load.sh                                # то же, что ночью: память → транскрипты → инбокс → склейка → контроль
tail -50 cron_load.log; cat cron_load.err           # результат; `!! batch` = сорванная порция (догрузится завтра)
# по одному источнику (env даёт lib.sh; ключ — из .env):
. scripts/lib.sh; $DEX /app/loaders/bulk_load.py --source memory --hash-names --dry-run
$GM_HOST_PY scripts/sessions_extract.py --source transcripts && $GM_HOST_PY scripts/sessions_extract.py --source transcripts --check
```

## Полная сборка / пересборка
```bash
scripts/backup.sh pre-build                                     # если граф уже есть и его перезаливают — снимок ДО
nohup setsid scripts/build_main.sh > build_main.log 2>&1 &     # по build.steps из конфига, идемпотентно
scripts/chain_watch.sh                                          # сторож (tmux / Monitor): PROGRESS, ALERT, DONE
grep -a -c "!! batch" build_main.log                            # сорванных порций (grep -a: лог «бинарный» из-за вывода модели)
```
**После сборки — добивка** (сорванные порции state не отмечает; загрузчик подхватит только их):
```bash
nohup setsid scripts/fixup.sh > fixup.log 2>&1 &               # порция 6, затем merge + scrub; повторить, если снова `!! batch`
LOG=fixup.log PROC=fixup.sh DONE_MARK="добивка завершена" scripts/chain_watch.sh
```
**Собрать main из существующего графа** (самый большой блок уже загружен в `<old>`):
```bash
$R GRAPH.COPY <old> main
$R GRAPH.QUERY main "MATCH (n) SET n.group_id = 'main' RETURN count(n)"          # Graphiti фильтрует по свойству group_id
$R GRAPH.QUERY main "MATCH ()-[r]->() SET r.group_id = 'main' RETURN count(r)"
cp state/<old>.json state/main.json                                              # чтобы блок не считался новым
```
затем убрать этот блок из `build.steps` (или оставить — state пропустит) и запустить `build_main.sh`.

## Склейка дублей и чистка
```bash
. scripts/lib.sh
$DEX /app/loaders/merge_aliases.py --graph main            # план (что с чем склеится)
$DEX /app/loaders/merge_aliases.py --graph main --apply    # применить; новые прозвища — в aliases конфига
$GM_HOST_PY scripts/scrub_graph.py --graphs main           # отчёт по секретам/телефонам
$GM_HOST_PY scripts/scrub_graph.py --graphs main --apply   # заменить на [REDACTED:…]
```
Перед первым боевым merge — репетиция: `$R GRAPH.COPY main main_test` → прогон на `main_test` → `$R GRAPH.DELETE main_test`.
**Дописать прозвища после сборки** (модель плодит варианты одной машины/человека):
```bash
$R GRAPH.QUERY main "MATCH (n:Server) RETURN n.name ORDER BY n.name" | grep -v -E "^(n.name|Cached|Query)"
$R GRAPH.QUERY main "MATCH (n:Human) RETURN n.name ORDER BY n.name"  | grep -v -E "^(n.name|Cached|Query)"
```
→ варианты («Амстердам-dev», «Aмстердам» латиницей, «старом dev», «двойник Алматы», логин e-mail владельца) — в `aliases`
конфига → `merge_aliases --apply`. Мусорные Server-узлы (IP Telegram, подсети 10.x, id серверов у провайдера) — известный
дефект дешёвой модели; лечится инструкцией извлечения, склейкой не трогаются.

## Снимок и откат
**Правило: снимок ПЕРЕД любым разрушительным** (GRAPH.DELETE, clear_graph, rm state/*, пересоздание контейнера, смена эмбеддера).
```bash
scripts/backup.sh <метка>                        # redis SAVE + копия dump.rdb (600) → backup.dir/dump-<дата>-<метка>.rdb, ретенция backup.keep
ls -lt $(scripts/gm_config.py get backup.dir)    # что есть
# откат: docker stop <c> && cp <снимок> <том>/dump.rdb && docker start <c>   (том: docker inspect -f '{{range .Mounts}}{{.Source}} {{.Destination}}\n{{end}}' <c>)
$R GRAPH.COPY main main_bak_$(date +%F)          # лёгкий вариант: копия одного графа внутри базы (рестарта не требует)
```
Удаление (в рамках поставленной задачи — после снимка; вне задачи — по явному «ок»):
```bash
$R GRAPH.DELETE <graph>; mv state/<graph>.json state/<graph>.json.bak-$(date +%F)   # снести граф; state — в сторону, не rm
scripts/mcp_client.py clear_graph '{}'            # MCP-вариант (граф по умолчанию)
```

## Бэкап / перенос
```bash
docker run --rm -v <compose>_falkordb_data:/d -v $PWD:/b alpine tar czf /b/falkordb-$(date +%F).tgz -C /d .   # том целиком
# восстановление: остановить контейнер, распаковать в том, поднять. Либо пересобрать из источников (build_main.sh).
```

## Контейнер и модели
```bash
scripts/backup.sh pre-restart                                # снимок перед пересозданием/рестартом
docker compose up -d; docker logs --tail 50 graphiti-mcp     # после старта: PONG ждём ~1 мин на RDB 360 МБ
docker inspect -f '{{.RestartCount}}' graphiti-mcp           # 0 ожидается; >0 = MCP стартует раньше базы (см. типовые ошибки)
docker logs graphiti-mcp 2>&1 | grep -a falkor_vector_patch  # патч в MCP-сервере включён (иначе поиск виснет)
scripts/gm_config.py render-compose > docker-compose.yml     # после правки конфига (порты, тома); затем backup.sh + compose up -d
scripts/tei.sh                                               # локальный эмбеддер для MCP
curl -s https://openrouter.ai/api/v1/auth/key -H "Authorization: Bearer $OPENAI_API_KEY"   # usage по ключу
```
Смена эмбеддера (модель/размерность) → `EMBEDDER_*` в конфиге → граф перезалить целиком.
Если FalkorDB завис (одно ядро 100 %, `redis-cli PING` молчит минуты — тяжёлый запрос без патча): убить загрузчики по PID
(`. scripts/lib.sh; gm_kill bulk_load` — исключает свою оболочку и её родителей; НЕ `pkill -f`: он убьёт и оболочку с тем же текстом в команде),
затем `docker restart -t 5 <c>`; RDB на диске автосохранён (потеря — незавершённая порция, state её не отметил → fixup).

## Крон
```
30 3 * * * <deploy_dir>/scripts/cron_load.sh >/dev/null 2>><deploy_dir>/cron_load.err # GRAPHITI_NIGHTLY_LOAD
```
`scripts/setup.sh --config … --cron` ставит; отключить — закомментировать строку с `GRAPHITI_NIGHTLY_LOAD`.

## Типовые ошибки
| Симптом | Причина → что делать |
|---|---|
| `Too many connections` | пул FalkorDB мал для потоков → `load.falkor_max_conn: 4096` |
| порция не заканчивается, JSON error по кругу | порция > 12 → `load.batch: 12` |
| `422 batch size N > 32` | локальный TEI, >32 текстов → `embed_chunk_patch` (уже включён), для bulk — облачный эмбеддер |
| `429 Model is overloaded` от TEI | массовая загрузка в локальный эмбеддер → `embedder.bulk_api_url` в облако |
| порции растут с графом, база ест ядра | нет векторного индекса → `load.vector_index: true` (патч) |
| `RediSearch: Syntax error` роняет порцию | спецсимволы в fulltext → патч отдаёт `[]` (уже включён) |
| `уже грузится другим процессом` | flock: второй загрузчик на ту же группу — дождаться |
| `Target entity not found`, `Unterminated string`, `invalid duplicate` | предупреждения graphiti/ретраи — не ошибки |
| «Амстердам» и «201.51.23.17» — две карточки | дедуп ищет по похожести имён → `aliases` + `merge_aliases` |
| `search_memory_facts` через MCP висит >120 с, FalkorDB 100 % одного ядра, PING молчит | патчи не стоят в MCP-сервере → в compose `PYTHONPATH=/app/loaders`, `GRAPHITI_PATCH=1`, `VECTOR_INDEX=1` (sitecustomize.py); проверка `docker logs … | grep falkor_vector_patch` |
| контейнер в цикле рестартов, в логах `FalkorDB is ready!` → `Creating OpenAI client` → тишина; RDB грузится заново | штатный start-services.sh принимает `LOADING` за готовность → наш `scripts/start-services.sh` как entrypoint (ждёт `PONG`) |
| `docker restart` → `mount … not a directory` | bind-mount одного файла, а на хосте файл переехал/стал каталогом → `docker compose up -d` с актуальным compose; монтировать каталоги |
| сторож молчит, а порция сорвана | строка `!! batch … Unterminated string` попадала под фильтр предупреждений → `gm_filt_raw` пропускает `!! batch` всегда; greps с `-a` |
| перезапущенный сторож повторно алертит старую ошибку | счётчик стартует с нуля → в `chain_watch.sh` начальное значение берётся из лога (уже исправлено) |
| `pkill -f run_x.sh` убил мою оболочку | шаблон совпал с командной строкой самой оболочки → `pgrep -f "[r]un_x"` по PID |
| `AsyncMessages.create() got an unexpected keyword argument 'temperature'` | нативный anthropic-клиент в образе сломан → `llm.provider: openai` + OpenAI-совместимый URL |
