"""
Leitura do /api/estoque do agente-estoque (API existente, só leitura — repo do Dayan
NÃO é tocado). Fornece o "estoque inicial" por cultivar pro consolidado:
produção (bag) + compras (compra_bag) − reprovação/qualidade (qualidade_bag).

Graceful degradation: se o agente-estoque estiver fora, devolve o último cache
(stale) ou vazio — o painel continua funcionando sem a coluna de estoque.
"""
import logging
import threading
import time

import requests

from config import CONFIG, ESTOQUE_CACHE_TTL

log = logging.getLogger("MetasComercial.Estoque")

_cache = {"data": None, "ts": 0}
_lock = threading.Lock()


def fetch_estoque_map(force: bool = False) -> dict:
    """{cultivar_norm: {producao_bag, compra_bag, qualidade_bag}}"""
    global _cache
    with _lock:
        now = time.time()
        if not force and _cache["data"] is not None and (now - _cache["ts"]) < ESTOQUE_CACHE_TTL:
            return _cache["data"]
    url = CONFIG["ESTOQUE_API_URL"].rstrip("/") + "/api/estoque"
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        payload = r.json()
        data = payload.get("data") or []
        mapa = {
            item["nome_norm"]: {
                "producao_bag":  float(item.get("bag") or 0),
                "compra_bag":    float(item.get("compra_bag") or 0),
                "qualidade_bag": float(item.get("qualidade_bag") or 0),
            }
            for item in data if item.get("nome_norm")
        }
        with _lock:
            _cache = {"data": mapa, "ts": time.time()}
        log.info(f"Estoque: {len(mapa)} cultivares lidas do agente-estoque.")
        return mapa
    except Exception as e:
        log.warning(f"agente-estoque indisponível ({e}) — usando cache stale/vazio.")
        return _cache.get("data") or {}
