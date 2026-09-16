"""SMALL_MODEL_NAME для MCP-сервера (16.09.2026).

MCP-фабрика (src/services/factories.py) жёстко ставит small_model = model, т.е. дедуп/противоречия в пути add_memory
шли на дешёвую основную модель. Решение владельца 16.09: на дедупе — умная модель (anthropic/claude-sonnet-5).
Патч: после штатного LLMConfig.__init__ подменяем small_model значением env SMALL_MODEL_NAME (если задано).
Загрузчики (bulk_load.py) читают тот же env сами; патч для них безвреден (то же значение)."""
import os

from graphiti_core.llm_client import config as _cfg

_small = os.environ.get('SMALL_MODEL_NAME')
if _small:
    _orig_init = _cfg.LLMConfig.__init__

    def _init(self, *a, **kw):
        _orig_init(self, *a, **kw)
        self.small_model = _small

    _cfg.LLMConfig.__init__ = _init
    print(f'small_model_patch: small_model={_small} для всех LLMConfig', flush=True)
