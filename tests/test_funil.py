"""Funil-espelho do agente-estoque: normalizações e extração de linhas."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sa_client import norm_status, norm_nome, extrair_linhas, agregar_pedido_cultivar
from config import STATUS_INCLUIDOS


def test_norm_status_remove_acentos_e_case():
    assert norm_status("Aguardando Aprovação") == "aguardando aprovacao"
    assert norm_status("  INTEGRADO ") == "integrado"
    assert norm_status("Aprovado") == "aprovado"
    assert norm_status("Cancelado") not in STATUS_INCLUIDOS


def test_norm_nome_igual_estoque():
    assert norm_nome("  neo700i2x ") == "NEO700I2X"
    assert norm_nome("85K84RSF  CE") == "85K84RSF CE"


def _pedido(num, status, itens, vendedor="JOSE OSVALDO", cliente="Cliente X"):
    return {
        "numero": num, "status_pedido": status, "order_created_at": "2026-07-01T10:00:00",
        "cliente": {"nome": cliente}, "vendedor": {"nome": vendedor},
        "filial": {"nome": "Matriz"}, "uso_semente": "PLANTIO",
        "itens": [{"produto": {"nome": c}, "quantidade": q} for c, q in itens],
    }


def test_extrair_linhas_funil():
    orders = [
        _pedido("P1", "Aprovado", [("NEO700I2X", 20), ("761I2X", 10)]),
        _pedido("P2", "Cancelado", [("NEO700I2X", 99)]),
        _pedido("P3", "Aguardando Aprovação", [("NEO700I2X", 5)]),
    ]
    linhas = extrair_linhas(orders)
    assert len(linhas) == 4
    vendido = sum(l["bags"] for l in linhas if l["incluido"])
    assert vendido == 35  # 20 + 10 + 5; cancelado fora
    cancelada = [l for l in linhas if l["numpedido"] == "P2"][0]
    assert cancelada["incluido"] is False


def test_quantidade_ja_em_bags_sem_conversao():
    """Regra do estoque: item.quantidade JÁ É bags — não converter."""
    orders = [_pedido("P1", "Integrado", [("O790IPRO", 12.5)])]
    linhas = extrair_linhas(orders)
    assert linhas[0]["bags"] == 12.5


def test_agregar_pedido_cultivar_soma_itens_da_mesma_cultivar():
    orders = [_pedido("P1", "Aprovado", [("NEO700I2X", 20), ("NEO700I2X", 15)])]
    agg = agregar_pedido_cultivar(extrair_linhas(orders))
    assert agg[("P1", "NEO700I2X")]["bags"] == 35
