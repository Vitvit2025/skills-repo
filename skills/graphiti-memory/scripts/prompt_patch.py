"""Подмена промптов ДЕДУПА graphiti-core 0.30.1 (16.09.2026).

Почему: `custom_extraction_instructions` доходит только до извлечения (extract_nodes/extract_edges); промпты
dedupe_nodes.node/nodes и dedupe_edges.resolve_edge — встроенные, наших правил не видят. Штатный resolve_edge учит
«engineer → senior engineer = противоречие» → уточнения гасили верные факты (17.7k invalid_at, 35 % в окне <1 дня);
dedupe_nodes склеивал однофамильцев (Баранов-автор ↔ Баранов-подрядчик) и нарицательные («банк» → Альфа-банк).

Проверено по коду: prompt_library — обёртки VersionWrapper на объектах PromptTypeWrapper; подменяем атрибуты.
Подключается через sitecustomize (GRAPHITI_PATCH=1) — действует и в загрузчиках, и в MCP-сервере.
"""
from typing import Any

from graphiti_core.prompts import lib as _lib
from graphiti_core.prompts.models import Message
from graphiti_core.prompts.prompt_helpers import to_prompt_json

# типы связей, у которых новое значение ВЫТЕСНЯЕТ старое (см. config.yaml edge_types)
EXCLUSIVE_RELATIONS = 'RUNS_ON, HOSTED_ON, LOCATED_IN, LISTENS_ON, CONFIGURED_WITH, MANAGED_BY, OWNED_BY, REPLACED_BY'


def resolve_edge(context: dict[str, Any]) -> list[Message]:
    return [
        Message(
            role='system',
            content='You are a fact deduplication and contradiction assistant for a knowledge graph about servers, services, '
            'bots and projects (texts are mostly Russian). NEVER mark a fact as contradicted merely because it is worded '
            'differently, is more or less specific, or is written in another language.',
        ),
        Message(
            role='user',
            content=f"""
<EXISTING FACTS>
{context['existing_edges']}
</EXISTING FACTS>

<FACT INVALIDATION CANDIDATES>
{context['edge_invalidation_candidates']}
</FACT INVALIDATION CANDIDATES>

<NEW FACT>
{context['new_edge']}
</NEW FACT>

The two lists have CONTINUOUS idx numbering: EXISTING FACTS first, then FACT INVALIDATION CANDIDATES.
IMPORTANT constraints:
- duplicate_facts: ONLY idx values from EXISTING FACTS (never from FACT INVALIDATION CANDIDATES)
- contradicted_facts: idx values from EITHER list

Decide for the NEW FACT:

1. DUPLICATE → duplicate_facts=[idx]: the NEW FACT states the SAME relationship with the SAME level of detail —
   paraphrase, reordering, Russian/English translation, synonyms (бот = Telegram-бот; сервис = systemd-сервис;
   «работает как» = «является»; «хранит в» = «пишет в»).

2. CONTRADICTION → contradicted_facts=[idx]: ONLY when the two facts CANNOT both be true at the same time:
   - the same subject and the same relationship now point to a DIFFERENT, mutually exclusive value
     (порт 18001 → 18004; сервер A → сервер B; модель X → модель Y; включено → выключено; работает → удалён/остановлен);
     exclusive relationships are typically: {EXCLUSIVE_RELATIONS};
   - the NEW FACT explicitly ends or replaces the old one: «больше не», «удалён», «остановлен», «заменён на»,
     «переехал», «закрыто», «устарело», «до <дата>».

3. NEITHER (both lists empty) in every other case, in particular:
   - the NEW FACT adds detail or is more specific (тип сервиса, путь к файлу, версия, дата, число) — this is a
     REFINEMENT, not a contradiction; both facts stay;
   - the NEW FACT is less specific than an existing one;
   - different aspects of the same two entities (X пишет в БД / X читает БД / X имеет доступ к БД);
   - facts about different machines or environments (прод 201.51.23.17 / двойник 80.90.182.136 / старый dev) that coexist;
   - a plan and its result (планировали поставить → поставили) — not a contradiction;
   - an attempt that failed («X не сработало из-за Y») does not contradict a fact that X exists or was tried.

When unsure choose NEITHER: marking a true fact as contradicted destroys history, keeping an extra fact is cheap.

<EXAMPLES>
EXISTING idx=0: "Тренажёр ЕГЭ является сервисом ege-bot"
NEW: "Бот @Social_Studies_rus_bot работает как systemd-сервис ege-bot"
Result: duplicate_facts=[0], contradicted_facts=[]   (same relationship, other wording)

EXISTING idx=0: "Бот пишет попытки в sqlite"
NEW: "Бот пишет попытки в sqlite /opt/ege-obsh/data/progress.db"
Result: duplicate_facts=[], contradicted_facts=[]   (refinement — keep both)

EXISTING idx=0: "Alice is an engineer"
NEW: "Alice is a senior engineer"
Result: duplicate_facts=[], contradicted_facts=[]   (more specific, NOT a contradiction)

EXISTING idx=0: "Учётный прокси слушает порт 18001"
NEW: "Учётный прокси слушает порт 18004"
Result: duplicate_facts=[], contradicted_facts=[0]   (same exclusive relationship, different value)

CANDIDATE idx=3: "Почта mail.ewa.pro принимается на 45.145.168.13"
NEW: "45.145.168.13 недоступен с 14.09.2026, MX ewa.pro не отвечает"
Result: duplicate_facts=[], contradicted_facts=[3]   (explicit end of the old state)

EXISTING idx=0: "Beget предоставляет VPS 83.222.24.231"
NEW: "VPS 83.222.24.231 отдан Артёму под новую базу Лиги"
Result: duplicate_facts=[], contradicted_facts=[]   (both true at once)
</EXAMPLES>
""",
        ),
    ]


_NODE_RULES = """
Entities are duplicates ONLY if they are the SAME real-world object.

MERGE (return the candidate_id) when:
- the same machine is named by IP, hostname or nickname (201.51.23.17 = прод = prod-ams = Амстердам-прод);
- the same person by full name or a known alias (Владелец = Виталий = owner); the same bot/service by @username,
  container or systemd unit name; the same thing in Russian/English or transliterated; an abbreviation of the same name;
- the CURRENT MESSAGE makes it unambiguous that a descriptive label («бот», «этот сервер») refers to exactly that
  named EXISTING ENTITY.

NEVER merge (return -1) when:
- only the surname or first name coincides but the role or context differs (Баранов — автор пособий по ЕГЭ vs
  Баранов В. Н. — подрядчик РЭП); different initials or patronymic → different people;
- the new entity is a GENERIC noun («бот», «банк», «сервер», «база», «скрипт», «проект») and the CURRENT MESSAGE does
  not say WHICH specific one — do NOT attach it to the nearest named entity;
- similar names but different kind of thing (Альфа-банк vs банк заданий; Java language vs Java island);
- related but distinct (a service and its database; a project and its bot; a server and a container on it;
  a company and a person working there).

If unsure → -1. An unmerged duplicate is cheap; a wrong merge poisons both entities and every fact attached to them.
"""

_NODE_EXAMPLES = """
<EXAMPLE>
NEW ENTITY: "прод" (Server)
EXISTING ENTITIES: [{"candidate_id": 0, "name": "201.51.23.17", "entity_types": ["Server"], "summary": "боевой сервер Timeweb Амстердам, прозвище прод"}]
Result: duplicate_candidate_id = 0 (nickname of the same machine)

NEW ENTITY: "Баранов" (Human) — in CURRENT MESSAGE: автор сборника «ЕГЭ-2027, 50 вариантов»
EXISTING ENTITIES: [{"candidate_id": 0, "name": "Баранов Владимир Николаевич", "entity_types": ["Human"], "summary": "подрядчик РЭП №28, получил 25.2 млн"}]
Result: duplicate_candidate_id = -1 (same surname, different role and context)

NEW ENTITY: "банк"
EXISTING ENTITIES: [{"candidate_id": 0, "name": "Альфа-банк", "entity_types": ["Company"]}, {"candidate_id": 1, "name": "bank.json", "entity_types": ["Tool"]}]
Result: duplicate_candidate_id = -1 (generic noun, message does not say which)

NEW ENTITY: "Java" (programming language)
EXISTING ENTITIES: [{"candidate_id": 0, "name": "Java", "entity_types": ["Location"], "summary": "An island in Indonesia"}]
Result: duplicate_candidate_id = -1 (same name, distinct things)

NEW ENTITY: "Owner"
EXISTING ENTITIES: [{"candidate_id": 0, "name": "Владелец", "entity_types": ["Human"], "summary": "владелец серверов и проектов"}]
Result: duplicate_candidate_id = 0 (translation of the same person)
</EXAMPLE>
"""


def node(context: dict[str, Any]) -> list[Message]:
    return [
        Message(
            role='system',
            content='You are an entity deduplication assistant for a knowledge graph about servers, services, bots, '
            'people and projects. NEVER fabricate entity names or mark distinct entities as duplicates. Prefer NOT merging when unsure.',
        ),
        Message(
            role='user',
            content=f"""
<PREVIOUS MESSAGES>
{to_prompt_json(context['previous_episodes'])}
</PREVIOUS MESSAGES>

<CURRENT MESSAGE>
{context['episode_content']}
</CURRENT MESSAGE>

<NEW ENTITY>
{to_prompt_json(context['extracted_node'])}
</NEW ENTITY>

<ENTITY TYPE DESCRIPTION>
{to_prompt_json(context['entity_type_description'])}
</ENTITY TYPE DESCRIPTION>

<EXISTING ENTITIES>
{to_prompt_json(context['existing_nodes'])}
</EXISTING ENTITIES>
{_NODE_RULES}
Task:
1. Compare the NEW ENTITY against each EXISTING ENTITY (identified by `candidate_id`).
2. If it is the same real-world object, return that `candidate_id`.
3. Return `duplicate_candidate_id = -1` when there is no match or you are unsure.
{_NODE_EXAMPLES}
""",
        ),
    ]


def nodes(context: dict[str, Any]) -> list[Message]:
    n = len(context['extracted_nodes'])
    return [
        Message(
            role='system',
            content='You are an entity deduplication assistant for a knowledge graph about servers, services, bots, '
            'people and projects. NEVER fabricate entity names or mark distinct entities as duplicates. Prefer NOT merging when unsure.',
        ),
        Message(
            role='user',
            content=f"""
<PREVIOUS MESSAGES>
{to_prompt_json(context['previous_episodes'])}
</PREVIOUS MESSAGES>

<CURRENT MESSAGE>
{context['episode_content']}
</CURRENT MESSAGE>

<ENTITIES>
{to_prompt_json(context['extracted_nodes'])}
</ENTITIES>

<EXISTING ENTITIES>
{to_prompt_json(context['existing_nodes'])}
</EXISTING ENTITIES>

Each of the above ENTITIES was extracted from the CURRENT MESSAGE. For each entity decide whether it is a duplicate
of an EXISTING ENTITY.
{_NODE_RULES}
Task:
ENTITIES contains {n} entities with IDs 0 through {n - 1}.
Your response MUST include EXACTLY {n} resolutions with IDs 0 through {n - 1}. Do not skip or add IDs.

For every entity provide:
- `id`: integer id from ENTITIES
- `name`: the best full name (preserve the original name unless the duplicate has a more complete name; for a
  machine prefer the IP address; for a person keep the most complete name)
- `duplicate_candidate_id`: the `candidate_id` of the matching EXISTING ENTITY, or -1
{_NODE_EXAMPLES}
""",
        ),
    ]


_lib.prompt_library.dedupe_edges.resolve_edge = _lib.VersionWrapper(resolve_edge)
_lib.prompt_library.dedupe_nodes.node = _lib.VersionWrapper(node)
_lib.prompt_library.dedupe_nodes.nodes = _lib.VersionWrapper(nodes)
print('prompt_patch: подменены dedupe_edges.resolve_edge, dedupe_nodes.node, dedupe_nodes.nodes (уточнение ≠ противоречие; однофамильцы/нарицательные не склеивать)', flush=True)
