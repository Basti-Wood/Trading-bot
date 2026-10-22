"""Record exactly which dated evidence a news decision used.

Inspired by TradingAgents' point-in-time data guards and recorded run settings;
implemented locally without importing or copying the reference framework.
"""
import hashlib
import json

PROMPT_VERSION = 2


def provenance(symbol, sources, as_of, requested_model, returned_model, forecast=None):
    encoded = json.dumps(sources, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return {'symbol': symbol, 'as_of': as_of.isoformat(), 'input_bar': (forecast or {}).get('bar_at'),
            'horizon': (forecast or {}).get('horizon'), 'requested_model': requested_model,
            'returned_model': returned_model, 'prompt_version': PROMPT_VERSION,
            'news_sha256': hashlib.sha256(encoded.encode()).hexdigest()}
