"""Diff de snapshots — o 'por que mudou'."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import diff_snapshots


def _row(bags, incluido=True, status="Aprovado", vendedor="JOSE", cliente="C"):
    return {"bags": bags, "incluido": incluido, "status_raw": status,
            "vendedor": vendedor, "cliente": cliente}


def test_pedido_novo():
    evs = diff_snapshots({}, {("P1", "NEO700I2X"): _row(20)})
    assert len(evs) == 1
    assert evs[0]["tipo"] == "NOVO" and evs[0]["delta_bags"] == 20


def test_ajuste_pos_credito_reduz():
    old = {("P1", "NEO700I2X"): _row(100)}
    new = {("P1", "NEO700I2X"): _row(50)}
    evs = diff_snapshots(old, new)
    assert evs[0]["tipo"] == "AJUSTE" and evs[0]["delta_bags"] == -50


def test_cancelamento_sai_do_funil():
    old = {("P1", "761I2X"): _row(30)}
    new = {("P1", "761I2X"): _row(30, incluido=False, status="Cancelado")}
    evs = diff_snapshots(old, new)
    assert evs[0]["tipo"] == "SAIU_FUNIL" and evs[0]["delta_bags"] == -30


def test_entrou_no_funil():
    old = {("P1", "761I2X"): _row(30, incluido=False, status="Em digitação")}
    new = {("P1", "761I2X"): _row(30, incluido=True)}
    evs = diff_snapshots(old, new)
    assert evs[0]["tipo"] == "ENTROU_FUNIL" and evs[0]["delta_bags"] == 30


def test_pedido_removido_do_sa():
    old = {("P1", "761I2X"): _row(30)}
    evs = diff_snapshots(old, {})
    assert evs[0]["tipo"] == "REMOVIDO" and evs[0]["delta_bags"] == -30


def test_sem_mudanca_sem_evento():
    old = {("P1", "761I2X"): _row(30)}
    new = {("P1", "761I2X"): _row(30)}
    assert diff_snapshots(old, new) == []


def test_excluido_que_some_nao_gera_evento():
    old = {("P9", "X"): _row(10, incluido=False, status="Cancelado")}
    assert diff_snapshots(old, {}) == []
