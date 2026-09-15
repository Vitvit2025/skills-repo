#!/usr/bin/env python3
"""(Опционально) Учётный прокси к OpenRouter для MCP-сервера: слушает порты (каждый = метка), форвардит на openrouter.ai,
добавляет usage.include=true (иначе OpenRouter не отдаёт цену) и пишет JSONL: ts, tag, path, model, токены, cost, ms.
Для МАССОВОЙ загрузки НЕ использовать: без keep-alive +30–50 % на вызов; загрузчики ходят в OpenRouter напрямую.
Запуск (systemd): python3 or_proxy.py <bind> <port:tag>… [--log <файл>]   напр. 172.17.0.1 18001:mcp --log /opt/graphiti/or_usage.jsonl
В compose/конфиге: llm.mcp_api_url: http://172.17.0.1:18001/v1"""
import json, sys, threading, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
UPSTREAM = 'https://openrouter.ai/api/v1'
LOG = 'or_usage.jsonl'
lock = threading.Lock()


def make_handler(tag):
    class H(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *a): pass
        def do_POST(self):
            n = int(self.headers.get('Content-Length') or 0); body = self.rfile.read(n)
            j = {}
            try:
                j = json.loads(body)
                if isinstance(j, dict) and 'messages' in j:
                    j.setdefault('usage', {})['include'] = True
                    # reasoning выключаем: Graphiti — извлечение по схеме, «размышления» только жгут токены/время
                    # Gemini 3.x: reasoning обязателен → минимальное усилие; остальные — выключить
                    if 'reasoning' not in j:
                        j['reasoning'] = {'effort': 'low'} if str(j.get('model', '')).startswith('google/gemini-3') else {'enabled': False}
                    body = json.dumps(j).encode()
            except Exception: pass
            path = self.path if self.path.startswith('/v1') else '/v1' + self.path
            req = urllib.request.Request(UPSTREAM + path[3:], data=body, method='POST')
            for h in ('Authorization', 'HTTP-Referer', 'X-Title'):
                if self.headers.get(h): req.add_header(h, self.headers[h])
            req.add_header('Content-Type', 'application/json')
            t0 = time.time()
            timeout = 15 if 'embedding' in path else 120  # хвосты OpenRouter режем — клиент ретраит
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    status, data, ctype = r.status, r.read(), r.headers.get('Content-Type', 'application/json')
            except urllib.error.HTTPError as e:
                status, data, ctype = e.code, e.read(), e.headers.get('Content-Type', 'application/json')
            except Exception as e:
                status = 504 if 'timed out' in str(e) else 502
                data, ctype = json.dumps({'error': {'message': str(e), 'type': 'proxy_timeout' if status == 504 else 'proxy_error'}}).encode(), 'application/json'
            try:
                rj = json.loads(data); u = rj.get('usage') or {}
                rec = {'ts': round(t0, 1), 'tag': tag, 'path': path, 'status': status,
                       'model': rj.get('model') or (j.get('model') if isinstance(j, dict) else None),
                       'pt': u.get('prompt_tokens'), 'ct': u.get('completion_tokens'),
                       'rt': (u.get('completion_tokens_details') or {}).get('reasoning_tokens'),
                       'cost': u.get('cost'), 'ms': int((time.time() - t0) * 1000)}
                if status != 200: rec['err'] = str(rj.get('error'))[:200]
                with lock:
                    with open(LOG, 'a') as f: f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            except Exception: pass
            self.send_response(status); self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
    return H


if __name__ == '__main__':
    args = sys.argv[1:]
    if '--log' in args: i = args.index('--log'); LOG = args[i + 1]; del args[i:i + 2]
    bind = args[0]
    for spec in args[1:]:
        port, tag = spec.split(':')
        srv = ThreadingHTTPServer((bind, int(port)), make_handler(tag)); srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start(); print('listen', bind, port, tag, flush=True)
    while True: time.sleep(3600)
