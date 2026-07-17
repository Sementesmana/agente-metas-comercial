"""
Cache leve em memória pros payloads de leitura do painel (TTL 60s).
Invalidação total em QUALQUER escrita (meta, cadastro) e ao fim de cada sync —
dado nunca fica velho depois de uma ação do usuário.
(1 worker gunicorn → 1 processo → sem problema de cache por-worker.)
"""
import threading
import time

TTL = 60  # segundos

_store: dict = {}
_lock = threading.Lock()


def get(key: str):
    with _lock:
        item = _store.get(key)
        if not item:
            return None
        ts, payload = item
        if time.time() - ts > TTL:
            _store.pop(key, None)
            return None
        return payload


def set(key: str, payload):
    with _lock:
        _store[key] = (time.time(), payload)
    return payload


def invalidate():
    with _lock:
        _store.clear()
