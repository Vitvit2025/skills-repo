#!/usr/bin/env python3
"""Фильтр секретов перед загрузкой текста в Graphiti (шаг 4 проекта graphiti-память).

Три слоя:
  1. СЛОВАРЬ — реальные значения секретов, собранные с хоста (env-файлы /etc/*, /root/*/.env*, .devmem-secrets,
     приватные ssh-ключи, config.toml supabase, credentials.json …). Точное совпадение → [REDACTED:known].
     Словарь живёт только в памяти процесса, на диск не пишется.
  2. РЕГУЛЯРКИ — форматы известных токенов (OpenRouter/OpenAI/Anthropic, Telegram-бот, JWT, Google, AWS, GitHub,
     VK, Bitrix-вебхук, PEM-ключи, user:pass@ в URL, Authorization: Bearer, KEY=VALUE с секретным именем ключа,
     русские «пароль: …», телефоны +7…).
  3. ЭНТРОПИЯ — длинные (≥24) высокоэнтропийные токены без пробелов/точек (base64/hex-подобные) → [REDACTED:entropy].

Использование как модуль:  from secret_filter import SecretFilter; sf = SecretFilter(); clean, stats = sf.redact(text)
CLI:  python3 secret_filter.py --self-test | --scan <file> [--show-context]
Проверка результата: sf.leaks(text) → список правил, которые ещё срабатывают (должен быть пуст).
"""
import glob, json, math, os, re, sys, collections

SECRET_FILES = [
    '/etc/2gis/*', '/etc/asocks/*', '/etc/beget/*', '/etc/chatgpt-drive-gw/*', '/etc/claude-inbox/*',
    '/etc/cleaning-tenders/*', '/etc/ewa-redaktor/*', '/etc/ewa-secrets/*', '/etc/fleet/*', '/etc/iproxy/*',
    '/etc/n8n/*', '/etc/patent-agent/*', '/etc/selectel/*', '/etc/smm-bot/*', '/etc/onec/*', '/etc/ozon/*',
    '/etc/grafana-gate/*', '/etc/kontur/*', '/etc/ewa/*', '/etc/ewa-avito/*', '/etc/canva.env', '/etc/mailserver/*',
    '/root/.devmem-secrets', '/root/.home-net-creds', '/root/.newsrv-api-token', '/root/n8n.env',
    '/root/mailserver/mailserver.env', '/root/serdce-stolicy/.kn-creds', '/root/.docker/.token_seed',
    '/root/ewa-research/.bot_research_coach_token', '/root/.claude/.credentials.json',
    '/root/supabase-local/supabase/config.toml', '/root/supabase-local/.env*', '/root/.ssh/*',
    '/root/*/.env', '/root/*/.env.*', '/root/*/*/.env', '/root/graphiti-mcp/.env',
]
SKIP_FILE_RE = re.compile(r'(\.lock$|_state\.json$|balance_state|\.pub$|known_hosts|authorized_keys|\.example$|vps_info\.json$|vps_rucopy_info\.json$)')
# имена ключей, значения которых точно НЕ секрет (host/port/id/model…) — берём только если значение похоже на секрет
NONSECRET_KEY_RE = re.compile(r'(HOST|PORT|URL|URI|USER|USERNAME|NAME|ID|IDS|PATH|DIR|CHAT|CHATS|MODEL|EMAIL|LOGIN|ENABLED|DEBUG|LEVEL|TZ|LANG|REGION|BUCKET|FOLDER|MODE|OWNER|ALLOWED_USERS|OWNER_IDS|VERSION|TIMEOUT|LIMIT|INTERVAL|DB|DATABASE|SCHEMA|TABLE|PREFIX|SUFFIX|FROM|TO|CC|DOMAIN|SCOPE|SCOPES|TYPE|AUDIENCE|ISSUER|PROJECT|ENDPOINT|ALGORITHM|SITE|CAMPAIGN|GROUP|EXPIRES.*|EXPIRY|_AT|TIME|DATE|_TS|FILE|NOTE|COMMENT|DESCRIPTION)$', re.I)
SECRET_KEY_RE = re.compile(r'(PASS|PASSWORD|PASSWD|PWD|SECRET|TOKEN|API_KEY|APIKEY|KEY|CREDENTIAL|CREDS|AUTH|COOKIE|SESSION|PRIVATE|SIGNATURE|SALT|SEED|TOTP|2FA|WEBHOOK|DSN|PSK|(?<![A-Z])PIN(?![A-Z])|PASSPHRASE)', re.I)
STOPWORDS = {'true', 'false', 'none', 'null', 'postgres', 'password', 'admin', 'root', 'localhost', 'changeme',
             'example', 'default', 'secret', 'token', 'production', 'development', 'utf-8', 'https', 'http',
             'local', 'dummy', 'placeholder', 'unused', 'disabled', 'redacted'}


def _classes(s):
    return sum(bool(re.search(p, s)) for p in (r'[a-z]', r'[A-Z]', r'\d', r'[^A-Za-z0-9]'))


def _entropy(s):
    if not s: return 0.0
    c = collections.Counter(s); n = len(s)
    return -sum(v / n * math.log2(v / n) for v in c.values())


def _looks_secret(v, key=''):
    """Стоит ли класть значение в словарь."""
    v = v.strip().strip('"\'')
    if len(v) < 4 or v.lower() in STOPWORDS: return False
    # 🔴 ключ с секретным именем (PASSWORD/PSK/2FA/TOKEN…) → значение секрет ВСЕГДА, без проверки «похоже ли»:
    # 16.09.2026 пароль gpg-архива (7 цифр) и 2FA-код отбрасывались правилом «только цифры = IP/порт/версия»
    if key and SECRET_KEY_RE.search(key) and not NONSECRET_KEY_RE.search(key):
        # кроме заглушек, путей к файлу с секретом (ROOT_PASS_FILE=/etc/…), имён переменных и русских описаний (community_token_note)
        return not v.startswith(('<', '${', '{{', '/', '~', '$')) and not re.fullmatch(r'[A-Z][A-Z0-9_]+', v) and not re.search(r'[А-Яа-яЁё]', v)
    if len(v) < 6: return False
    if re.fullmatch(r'[A-Z][A-Z0-9_]+', v): return False  # имя переменной окружения, не значение
    if re.search(r'[А-Яа-яЁё]', v): return False  # секреты — ASCII; кириллица = описание
    if re.fullmatch(r'[\w-]+\.(?:md|py|sh|json|env|txt|yaml|yml|toml|log|conf|service|timer)', v): return False
    if v.startswith(('/', '~', '$', '<', '{', '[')) or v.endswith('.md'): return False
    if re.match(r'^https?://', v) and '@' not in v: return False
    if re.match(r'^[\d.:/]+$', v): return False  # IP, порт, версия, CIDR
    if re.match(r'^[\w.-]+@[\w.-]+\.\w+$', v): return False  # e-mail
    if re.fullmatch(r'[\w-]+(?:\.[\w-]+)*\.(?:ru|com|pro|io|net|org|dev|su|kz|cloud|app)', v): return False  # домен
    if re.fullmatch(r'[\w.-]+(?:/[\w.-]+)+', v): return False  # путь / имя модели вида vendor/model
    if re.fullmatch(r'@?\w+[Bb]ot', v): return False  # имя телеграм-бота
    if key and NONSECRET_KEY_RE.search(key): return False  # MODEL/ID/PROJECT/FOLDER/… — не секреты
    e = _entropy(v)
    return (_classes(v) >= 3 and len(v) >= 8 and e >= 3.0) or (len(v) >= 20 and e >= 3.8) or (len(v) >= 32 and e >= 3.5)


def _bare_token_secret(tok, single=False, comment=False):
    """Токен из свободного текста / комментария / строки без KEY=VALUE: только если похож на секрет по форме."""
    tok = tok.strip('()[]{}<>«»„“”"\',.;:!?')
    if len(tok) < 6 or not _looks_secret(tok): return False
    if tok.startswith(('@', './', '../')) or re.fullmatch(r'[\w.-]+\.(?:ru|com|pro|io|net|org|dev)', tok): return False
    if single: return True
    e = _entropy(tok); has_digit = bool(re.search(r'\d', tok))
    if comment:  # в комментариях в основном описания — берём только явно «ключеподобное»
        return (has_digit and _classes(tok) >= 3 and len(tok) >= 12 and e >= 3.5) or (len(tok) >= 24 and e >= 4.2)
    return (_classes(tok) >= 3 and len(tok) >= 10 and e >= 3.3) or (len(tok) >= 20 and e >= 3.8 and has_digit)


def _json_strings(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                if _looks_secret(v, k): out.add(v)
            else: _json_strings(v, out)
    elif isinstance(obj, list):
        for v in obj: _json_strings(v, out)
    elif isinstance(obj, str) and _looks_secret(obj): out.add(obj)


def build_dictionary(files=SECRET_FILES):
    vals = set(); n_files = 0
    for pat in files:
        for p in glob.glob(pat):
            if not os.path.isfile(p) or SKIP_FILE_RE.search(p) or os.path.getsize(p) > 200_000: continue
            try: raw = open(p, errors='ignore').read()
            except Exception: continue
            n_files += 1
            s = raw.strip()
            if s.startswith(('{', '[')):
                try: _json_strings(json.loads(s), vals); continue
                except Exception: pass
            lines = [l for l in raw.splitlines() if l.strip()]
            if len(lines) == 1 and ' ' not in s and (len(s) >= 16 or _looks_secret(s)): vals.add(s); continue  # файл = один токен (не логин из 5 букв)
            if '-----BEGIN' in raw:  # приватный ключ: каждая строка тела
                for l in lines:
                    if len(l.strip()) >= 20 and not l.startswith('-----'): vals.add(l.strip())
                continue
            for l in lines:
                is_comment = l.lstrip().startswith(('#', ';', '//'))
                body = l.lstrip('#; /') if is_comment else l
                if not is_comment and '#' in body and not body.strip().startswith('#'):
                    body = body.split('#', 1)[0]  # хвостовой комментарий
                m = re.match(r'^\s*(?:export\s+)?([A-Za-z_][\w.-]*)\s*[=:]\s*(.*)$', body)
                if m and not is_comment:
                    k, v = m.group(1), m.group(2).strip().strip('"\'')
                    if _looks_secret(v, k): vals.add(v)
                    # url с паролем внутри значения
                    for um in re.finditer(r'://([^/\s:@]+):([^@\s]+)@', v): vals.add(um.group(2))
                    # value может быть "user:pass" / телеграм-токен "botid:secret": первая половина (логин, id бота) — не секрет,
                    # берём её только если сама похожа на секрет; последняя — по правилу ключа
                    if ':' in v and ' ' not in v and not v.startswith('http'):
                        parts = v.split(':')
                        for i, part in enumerate(parts):
                            if _looks_secret(part, k if i == len(parts) - 1 else ''): vals.add(part)
                else:
                    toks = [t for t in re.split(r'[\s,;"\']+', body) if t.strip()]
                    single = (not is_comment and len(toks) == 1)
                    for tok in toks:
                        tok = tok.strip()
                        if _bare_token_secret(tok, single, is_comment):
                            vals.add(tok.strip('()[]{}<>«»„“”"\',.;:!?'))
                            if ':' in tok and not tok.startswith('http'):
                                for part in tok.split(':'):
                                    if _bare_token_secret(part, single, is_comment): vals.add(part)
                        for um in re.finditer(r'://([^/\s:@]+):([^@\s]+)@', tok): vals.add(um.group(2))
    vals = {v for v in vals if len(v) >= 4}  # короткие (PIN/2FA) — с границами слова в known_re, см. SecretFilter
    return vals, n_files


PATTERNS = [
    ('pem', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S)),
    ('openrouter', re.compile(r'sk-or-v1-[0-9a-f]{20,}')),
    ('anthropic', re.compile(r'sk-ant-[A-Za-z0-9_-]{20,}')),
    ('openai', re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}')),
    ('telegram', re.compile(r'\b\d{8,11}:[A-Za-z0-9_-]{30,45}\b')),
    ('jwt', re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}')),
    ('google', re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b')),
    ('google_oauth', re.compile(r'\b(?:ya29\.|1//)[A-Za-z0-9_-]{30,}')),
    ('aws', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('github', re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}\b')),
    ('slack', re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}')),
    ('vk', re.compile(r'\bvk1\.a\.[A-Za-z0-9_-]{40,}')),
    ('yandex_oauth', re.compile(r'\by[0-3]_[A-Za-z0-9_-]{40,}')),
    ('bitrix_webhook', re.compile(r'(/rest/\d+/)[a-z0-9]{12,}(/)')),
    ('url_creds', re.compile(r'(://[^/\s:@]+:)[^@\s/]+(@)')),
    ('bearer', re.compile(r'(?i)(authorization:\s*(?:bearer|basic|token)\s+)[A-Za-z0-9._=+/-]{8,}')),
    ('sshpass', re.compile(r"(sshpass\s+-p\s*)['\"]?[^\s'\"]+['\"]?")),
    ('pgpass', re.compile(r'(\b(?:PGPASSWORD|MYSQL_PWD|REDISCLI_AUTH)=)[^\s;&|]+')),
    ('kv', re.compile(r'(?i)(\b[\w.-]*(?:password|passwd|pass|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|client[_-]?secret|auth[_-]?key|webhook|totp|cookie)(?![a-z])[\w.-]*\s*[=:]\s*["\']?)(?!\[REDACTED)[^\s"\'`,;<>&]{6,}')),
    # 🔴 значение в блоке кода отдельной строкой после слов «пароль/password/PSK/PIN/Wi-Fi» (утечка пароля Wi-Fi 16.09.2026:
    # «пароль Wi-Fi сейчас такой:\n```\nXXXX\n```» — ни одна регулярка не дотягивалась). Группы: префикс и суффикс, значение между ними.
    ('codeblock_pw', re.compile(r'(?i)((?:парол\w*|password|passphrase|\bpsk\b|pin-?код|\bpin\b|wi-?fi|пароль от)[^\n]{0,100}\n[ \t]*```[a-z]*[ \t]*\n[ \t]*)[^\s`]{4,64}([ \t]*\n[ \t]*```)')),
    # «пароль Wi-Fi сейчас такой: XXXX», «PSK: XXXX» — между словом и двоеточием допускаем до 50 символов контекста
    # контекст не может содержать «@» — иначе «токен @Social_Studies_rus_bot» резал имя бота (16.09)
    ('kv_ru', re.compile(r'(?i)((?:парол[ьяюеи]\w*|пасс|токен|секрет|ключ api|api[- ]ключ|\btotp|\b2fa|\bpsk|pin-?код)(?:[^\n:=—–`"\'@]{0,50}?)\s*[:=—–-]?\s*[`"\']?)(?!\[REDACTED)(?![/~$<{\[@])[A-Za-z0-9!#$%^&*()_+=./-]{6,}')),
    ('phone', re.compile(r'(?<![\d.])(?:(?:\+7|8|7)[\s(-]?\d{3}[\s)-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}|\+\d{9,14})(?![\d.])')),
    ('hex32', re.compile(r'\b[0-9a-f]{32,}\b')),
]
KV_VALUE_SKIP = re.compile(r'^(?:\[REDACTED|/|~|\$|<|\{|https?://[^@]*$|\d+[.:][\d.:]*$|true$|false$|none$|null$|\w+\.(?:md|py|sh|json|env|txt|yaml|yml|toml)$)', re.I)  # IP/порт/версия — с точкой/двоеточием; чисто цифровой PIN после «пароль:» — секрет
ENTROPY_TOKEN = re.compile(r'(?<![\w/.:=-])[A-Za-z0-9+/=_-]{24,}(?![\w/.:=-])')


class SecretFilter:
    def __init__(self, with_dictionary=True):
        self.known, self.n_files = (build_dictionary() if with_dictionary else (set(), 0))
        # длинные значения первыми, чтобы «user:pass» не порвать на части
        self.known_sorted = sorted(self.known, key=len, reverse=True)
        # короткие буквенно-цифровые значения (<8) — только целым словом, иначе PIN «4471902» порежет любой id с этой подстрокой
        def _alt(v):
            return (r'(?<![A-Za-z0-9])' + re.escape(v) + r'(?![A-Za-z0-9])') if len(v) < 8 and v.isalnum() else re.escape(v)
        self.known_re = re.compile('|'.join(_alt(v) for v in self.known_sorted)) if self.known else None

    def redact(self, text, exclude=()):
        """exclude — имена правил, которые не применять (для прозы/графа обычно 'kv_ru': слишком шумное)."""
        stats = collections.Counter()
        if self.known_re:
            text, n = self.known_re.subn('[REDACTED:known]', text); stats['known'] += n
        for name, rx in PATTERNS:
            if name in exclude: continue
            if name in ('kv', 'kv_ru'):
                def _sub(m):
                    val = m.group(0)[len(m.group(1)):]
                    if KV_VALUE_SKIP.match(val): return m.group(0)
                    # kv_ru: «токен claud_nkt_bot» / «пароль brother_ro» — слово-имя, не значение; значение = есть цифра/спецсимвол или ≥3 класса
                    if name == 'kv_ru' and not (re.search(r'[\d!@#$%^&*()+=]', val) or _classes(val) >= 3) : return m.group(0)
                    stats[name] += 1; return m.group(1) + f'[REDACTED:{name}]'
                text = rx.sub(_sub, text)
            elif rx.groups:
                def _sub2(m, name=name):
                    stats[name] += 1
                    g = [x for x in m.groups() if x]
                    return g[0] + f'[REDACTED:{name}]' + (g[1] if len(g) > 1 else '')
                text = rx.sub(_sub2, text)
            else:
                text, n = rx.subn(f'[REDACTED:{name}]', text); stats[name] += n
        def _ent(m):
            t = m.group(0)
            if t.startswith('REDACTED') or '[REDACTED' in t: return t
            if re.fullmatch(r'[A-Za-z_-]+', t) or re.fullmatch(r'[\d.-]+', t): return t  # слово/число
            if t.count('-') >= 4 and re.fullmatch(r'[0-9a-f-]+', t): return t  # uuid
            if re.fullmatch(r'[a-z0-9]+(?:[_-][a-z0-9]+){2,}', t.lower()) and re.search(r'[a-z]{3,}', t): return t  # snake_case/kebab имя файла/памяти
            if _entropy(t) >= 4.2 and re.search(r'\d', t) and re.search(r'[A-Za-z]', t):
                stats['entropy'] += 1; return '[REDACTED:entropy]'
            return t
        text = ENTROPY_TOKEN.sub(_ent, text)
        return text, stats

    def leaks(self, text):
        """Что ещё срабатывает на уже очищенном тексте (ожидается пусто)."""
        found = []
        if self.known_re and self.known_re.search(text): found.append('known')
        for name, rx in PATTERNS:
            if name in ('kv', 'kv_ru', 'phone'): continue
            for m in rx.finditer(text):
                if '[REDACTED' not in m.group(0): found.append(name); break
        return found


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument('--self-test', action='store_true'); ap.add_argument('--scan')
    ap.add_argument('--show-context', action='store_true'); a = ap.parse_args()
    sf = SecretFilter()
    print(f'словарь: {len(sf.known)} значений из {sf.n_files} файлов', file=sys.stderr)
    if a.self_test:
        sample = ('ключ OPENROUTER_API_KEY=sk-or-v1-' + 'a1b2c3d4' * 8 + ' и бот 123456789:AAHfz9Q-abcdefghijklmnopqrstuvwxyz012345 '
                  'jwt eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYW5vbiJ9.abcdefghijklmnop12345 пароль: Qwe12345! '
                  'psql postgresql://pokos:S3cr3tPass@127.0.0.1:5433/pokos вебхук https://x.bitrix24.ru/rest/1/abcdef1234567890/crm.deal.list '
                  'путь /root/pokos-mvp/core/geo/design_sun.py ip 201.51.23.17 файл project_passport_philosophy.md '
                  'том graphiti-mcp_falkordb_data uuid f8aa1ee6-cc8e-4726-89b6-f4e4702a9c71 wg key 6GdY+lmR2sXk9pQz3tVb1nCw8eHf0aJu5yLo4iMr7Nk= '
                  'sha 3b2f9c1e4d5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c телефон +7 918 123-45-67 chat_id 159465823')
        out, st = sf.redact(sample); print(out); print(dict(st))
        # 🔴 обязательные случаи после утечек 16.09.2026 (значения выдуманы): цифровой пароль из хранилища, пароль в блоке кода, PSK с контекстом
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.env', delete=False) as tf:
            tf.write('ARCHIVE_GPG_PASSWORD=4471902   # 7 цифр\nROUTER_WIFI_PSK=kqZ7wpTa\nMTPROTO_2FA=88431\nOWNER_LOGIN=admin\nDB_HOST=127.0.0.1\n'); tfp = tf.name
        d, _ = build_dictionary([tfp]); os.unlink(tfp)
        assert {'4471902', 'kqZ7wpTa', '88431'} <= d and 'admin' not in d and '127.0.0.1' not in d, f'словарь: {d}'
        sf2 = SecretFilter(with_dictionary=False)
        cases = {'пароль Wi-Fi сейчас такой:\n```\nmxQ9vLp2\n```\nВведи его': 'mxQ9vLp2',
                 'В конфиге роутера пароль Wi-Fi: `mxQ9vLp2` и всё': 'mxQ9vLp2',
                 'PSK для гостевой сети = Tr4ns1tPass': 'Tr4ns1tPass',
                 'PIN-код от роутера: 4471902': '4471902'}
        bad = [t for t, v in cases.items() if v in sf2.redact(t)[0]]
        ok_noise = ['пароль сменить через 30 минут', 'токен claud_nkt_bot протух', 'пароль от grafana — admin', 'файл project_x_2026-07-11.md',
                    'перевыпустить токен @Social_Studies_rus_bot через BotFather', 'токен бота @ewankt_bot лежит в /etc/claude-inbox/env']
        noisy = [t for t in ok_noise if sf2.redact(t)[0] != t]
        assert not bad and not noisy, f'не поймано: {bad}; ложные: {noisy}'
        print('self-test: пароли из хранилища, блок кода, PSK/PIN с контекстом — OK; ложных срабатываний на шуме нет')
    if a.scan:
        txt = open(a.scan, errors='ignore').read(); out, st = sf.redact(txt)
        print(dict(st)); print('утечки после фильтра:', sf.leaks(out))
        if a.show_context:
            for m in list(re.finditer(r'\[REDACTED:\w+\]', out))[:60]:
                print('…' + out[max(0, m.start() - 50):m.end() + 30].replace('\n', ' ') + '…')
