"""Локальный TEI принимает ≤32 текстов за запрос (--max-client-batch-size по умолчанию),
а bulk-путь Graphiti шлёт create_batch на 35–45 текстов → 422 «batch size N > maximum allowed batch size 32».
Патч: режем create_batch на куски по EMBED_MAX_BATCH (по умолчанию 32) и склеиваем результат."""
import os
from graphiti_core.embedder.openai import OpenAIEmbedder

MAX = int(os.environ.get('EMBED_MAX_BATCH', '32'))
_orig = OpenAIEmbedder.create_batch


async def create_batch(self, input_data_list):
    if len(input_data_list) <= MAX:
        return await _orig(self, input_data_list)
    out = []
    for i in range(0, len(input_data_list), MAX):
        out.extend(await _orig(self, input_data_list[i:i + MAX]))
    return out


OpenAIEmbedder.create_batch = create_batch
