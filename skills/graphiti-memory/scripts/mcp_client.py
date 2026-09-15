#!/usr/bin/env python3
"""Мини-клиент MCP (streamable HTTP) для Graphiti: initialize → tools/list → tools/call. Как модуль или CLI:
  python3 mcp_client.py                       # серверная информация и список инструментов
  python3 mcp_client.py get_status '{}'
  python3 mcp_client.py search_memory_facts '{"query":"что зависит от прокси","max_facts":10}'
URL: env MCP_URL, иначе http://127.0.0.1:<ports.mcp>/mcp из graphiti-memory.yaml, иначе :8000 (без слэша в конце — со слэшем 307)."""
import json, os, sys, urllib.request, itertools
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _url():
    if os.environ.get('MCP_URL'): return os.environ['MCP_URL']
    try:
        from gm_config import cfg
        return f"http://127.0.0.1:{cfg().get('ports.mcp', 8000)}/mcp"
    except SystemExit:
        return 'http://127.0.0.1:8000/mcp'


_id = itertools.count(1)


class MCP:
    def __init__(self, url=None):
        self.url = url or _url(); self.sid = None
        r = self._post({'jsonrpc': '2.0', 'id': next(_id), 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'graphiti-memory-skill', 'version': '0.1'}}})
        self._post({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, notify=True)
        self.server = r.get('result', {}).get('serverInfo')

    def _post(self, body, notify=False):
        h = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
        if self.sid: h['Mcp-Session-Id'] = self.sid
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=h, method='POST')
        with urllib.request.urlopen(req, timeout=120) as resp:
            sid = resp.headers.get('Mcp-Session-Id')
            if sid: self.sid = sid
            raw = resp.read().decode()
            if notify: return None
            ctype = resp.headers.get('Content-Type', '')
        if 'text/event-stream' in ctype:
            for line in raw.splitlines():
                if line.startswith('data:'):
                    try: return json.loads(line[5:].strip())
                    except Exception: pass
            return {}
        return json.loads(raw) if raw else {}

    def tools(self):
        return [t['name'] for t in self._post({'jsonrpc': '2.0', 'id': next(_id), 'method': 'tools/list'}).get('result', {}).get('tools', [])]

    def call(self, tool, **args):
        r = self._post({'jsonrpc': '2.0', 'id': next(_id), 'method': 'tools/call', 'params': {'name': tool, 'arguments': args}})
        res = r.get('result') or r
        out = [c['text'] for c in (res.get('content', []) if isinstance(res, dict) else []) if c.get('type') == 'text']
        return '\n'.join(out) if out else json.dumps(res, ensure_ascii=False)[:2000]


if __name__ == '__main__':
    m = MCP()
    if len(sys.argv) > 1: print(m.call(sys.argv[1], **json.loads(sys.argv[2] if len(sys.argv) > 2 else '{}')))
    else: print('server:', m.server); print('tools:', m.tools())
