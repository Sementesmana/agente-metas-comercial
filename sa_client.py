"""
Cliente Simple Agro — ESPELHO do modelo do agente-estoque (produção).

⚠️ NÃO INOVAR AQUI. Este módulo replica 1:1 a forma como o agente-estoque
lê e filtra pedidos do SA, para que o realizado do painel de metas seja
O MESMO NÚMERO do painel de estoque:
  - login com XSRF + JWT
  - GET /api/orders  (safra.id, itens.grupo_produto.id, deleted=false, limit=-1)
  - quantidade do item JÁ É EM BAGS (item.quantidade)
  - conta como vendido apenas status ∈ STATUS_INCLUIDOS (normalizado sem acento)
  - norm de cultivar: uppercase + espaços colapsados
"""
import logging
import re
import threading
import time
import unicodedata
from urllib.parse import unquote

import requests

from config import CONFIG, STATUS_INCLUIDOS, SA_CACHE_TTL

log = logging.getLogger("MetasComercial.SA")

_cache: dict = {}   # {sa_safra_id: {"data": [...], "ts": epoch}}
_cache_lock = threading.Lock()


# ── Normalizações (idênticas ao agente-estoque) ───────────────────────────────

def norm_status(s: str) -> str:
    """lowercase, sem acentos, sem espaços duplos."""
    s = unicodedata.normalize("NFD", (s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip()


def norm_nome(nome: str) -> str:
    """uppercase, espaços colapsados."""
    return re.sub(r"\s+", " ", (nome or "").upper().strip())


def _extract_nome(val) -> str:
    if isinstance(val, dict):
        return (val.get("label") or val.get("nome") or val.get("value") or "").strip()
    return str(val or "").strip()


def _safe_float(val) -> float:
    try:
        return float(str(val or 0).replace(",", "."))
    except Exception:
        return 0.0


# ── Client (espelho do agente-estoque / agente-integracoes) ───────────────────

class SimpleAgroClient:
    def __init__(self):
        self.base = CONFIG["SA_BASE_URL"].rstrip("/")
        self.token = None
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _get_xsrf_token(self) -> str:
        login_page = CONFIG["SA_BASE_URL"].replace(
            "sementesmana.api.simpleagro.com.br:3333",
            "sementesmana.painel.simpleagro.com.br:3333"
        ) + "/sales/login"
        try:
            self.session.get(login_page, timeout=15)
            token = self.session.cookies.get("XSRF-TOKEN", "")
            if token:
                token = unquote(token)
                self.session.headers.update({"X-XSRF-TOKEN": token})
            return token
        except Exception as e:
            log.warning(f"XSRF token: {e}")
            return ""

    def login(self) -> bool:
        if not CONFIG["SA_USERNAME"] or not CONFIG["SA_PASSWORD"]:
            log.error("SA_USERNAME / SA_PASSWORD não configurados!")
            return False
        self._get_xsrf_token()
        self.session.headers.update({
            "Origin":  "https://sementesmana.painel.simpleagro.com.br:3333",
            "Referer": "https://sementesmana.painel.simpleagro.com.br:3333/sales/login",
        })
        url = f"{self.base}/api/auth/login"
        body = {"login": CONFIG["SA_USERNAME"], "senha": CONFIG["SA_PASSWORD"]}
        try:
            r = self.session.post(url, json=body, timeout=30)
            r.raise_for_status()
            data = r.json()
            raw = (data.get("token") or data.get("accessToken") or
                   data.get("access_token") or (data.get("data") or {}).get("token"))
            if not raw:
                log.error(f"Token não encontrado. Chaves: {list(data.keys())}")
                return False
            self.token = raw if raw.startswith("Bearer ") else f"Bearer {raw}"
            self.session.headers.update({"Authorization": self.token})
            return True
        except Exception as e:
            log.error(f"Erro login SA: {e}")
            return False

    def get_orders(self, sa_safra_id: str | None = None) -> list:
        url = f"{self.base}/api/orders"
        params = {
            "safra.id":               sa_safra_id or CONFIG["SAFRA_ID"],
            "limit":                  -1,
            "itens.grupo_produto.id": CONFIG["GRUPO_SOJA_ID"],
            "deleted":                "false",
        }
        try:
            r = self.session.get(url, params=params, timeout=120)
            r.raise_for_status()
            data = r.json()
            orders = (data if isinstance(data, list)
                      else data.get("data") or data.get("docs") or data.get("orders") or [])
            log.info(f"SA: {len(orders)} pedidos recebidos.")
            return orders
        except Exception as e:
            log.error(f"Erro ao buscar pedidos SA: {e}")
            return []


# ── Extração de linhas normalizadas (pedido × cultivar) ───────────────────────

def extrair_linhas(orders: list) -> list:
    """
    Converte pedidos brutos do SA em linhas pedido×item normalizadas.
    Mesma lógica de extração do fetch_sa_vendas do agente-estoque.

    Retorna lista de dicts:
      {numpedido, data, cliente, vendedor, filial, uso_semente,
       cultivar_nome, cultivar_norm, bags, status_raw, status_norm, incluido}
    """
    linhas = []
    for order in orders:
        status_raw = _extract_nome(order.get("status_pedido") or order.get("status") or "")
        status_low = norm_status(status_raw)
        num_pedido = str(order.get("numero") or order.get("numpedido") or "")
        data_ped = str(order.get("order_created_at") or order.get("created_at") or "")[:10]

        cliente_obj = order.get("cliente") or {}
        cliente = (cliente_obj.get("nome") or cliente_obj.get("name") or "") \
            if isinstance(cliente_obj, dict) else str(cliente_obj)

        vendedor_obj = order.get("vendedor") or {}
        vendedor = (vendedor_obj.get("nome") or vendedor_obj.get("name") or "") \
            if isinstance(vendedor_obj, dict) else str(vendedor_obj)

        filial_obj = order.get("filial") or {}
        filial = (filial_obj.get("nome") or "") \
            if isinstance(filial_obj, dict) else str(filial_obj or "")

        uso_semente = str(order.get("uso_semente") or "").strip()

        for item in (order.get("itens") or order.get("items") or []):
            if not isinstance(item, dict):
                continue
            prod_obj = item.get("produto") or {}
            nome_sa = (prod_obj.get("nome") or "") if isinstance(prod_obj, dict) else str(prod_obj or "")
            nome_sa = nome_sa.strip()
            if not nome_sa:
                nome_sa = _extract_nome(item.get("produto_nome") or item.get("product_name") or "")
            if not nome_sa:
                continue

            qtd = _safe_float(item.get("quantidade"))  # JÁ EM BAGS (regra do estoque)
            if qtd == 0:
                continue

            linhas.append({
                "numpedido":     num_pedido,
                "data":          data_ped,
                "cliente":       cliente.strip(),
                "vendedor":      vendedor.strip(),
                "filial":        filial.strip(),
                "uso_semente":   uso_semente,
                "cultivar_nome": nome_sa,
                "cultivar_norm": norm_nome(nome_sa),
                "bags":          qtd,
                "status_raw":    status_raw,
                "status_norm":   status_low,
                "incluido":      status_low in STATUS_INCLUIDOS,
            })
    return linhas


def agregar_pedido_cultivar(linhas: list) -> dict:
    """
    Agrega linhas por (numpedido, cultivar_norm) — chave estável do diff.
    Retorna {(numpedido, cultivar_norm): {bags, incluido, status_raw, vendedor,
             cliente, data, uso_semente, filial, cultivar_nome}}
    """
    agg = {}
    for ln in linhas:
        key = (ln["numpedido"], ln["cultivar_norm"])
        if key not in agg:
            agg[key] = dict(ln)
        else:
            agg[key]["bags"] += ln["bags"]
    return agg


def fetch_linhas_sa(force: bool = False, sa_safra_id: str | None = None) -> list:
    """Busca (com cache 30 min POR SAFRA, igual estoque) e devolve linhas normalizadas."""
    global _cache
    sid = sa_safra_id or CONFIG["SAFRA_ID"]
    with _cache_lock:
        now = time.time()
        item = _cache.get(sid)
        if not force and item and (now - item["ts"]) < SA_CACHE_TTL:
            log.info("SA: usando cache.")
            return item["data"]
    client = SimpleAgroClient()
    if not client.login():
        log.error("SA: login falhou — devolvendo cache antigo ou vazio.")
        return (_cache.get(sid) or {}).get("data") or []
    orders = client.get_orders(sid)
    if not orders:
        return (_cache.get(sid) or {}).get("data") or []
    linhas = extrair_linhas(orders)
    with _cache_lock:
        _cache[sid] = {"data": linhas, "ts": time.time()}
    total = sum(l["bags"] for l in linhas if l["incluido"])
    log.info(f"SA processado (safra {sid}): {len(linhas)} linhas, {total:.1f} bags no funil.")
    return linhas
