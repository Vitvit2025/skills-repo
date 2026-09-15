# skills-repo — скиллы для Claude Code (раздача коллегам)

Ставятся через [skills CLI](https://skills.sh):

```bash
npx skills add Vitvit2025/skills-repo@graphiti-memory -g -y     # → ~/.agents/skills/graphiti-memory (+ симлинк в ~/.claude/skills)
npx skills update                                            # обновить все
```

| Скилл | Что делает |
|---|---|
| [`graphiti-memory`](skills/graphiti-memory/SKILL.md) | Графовая долговременная память для Claude Code на своём сервере (Graphiti + FalkorDB + MCP): развернуть, загрузить корпус через фильтр секретов, работать в сессии (поиск/запись), ночная докачка. |

Перед установкой любого стороннего скилла читай его SKILL.md и скрипты целиком — он работает с полными правами агента.
