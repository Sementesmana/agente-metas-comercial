"""Rateio do cascateamento."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from metas_service import ratear


def test_igual_por_cabeca_fecha_conta():
    deltas = ratear(100, [1, 2, 3], "igual", {})
    assert round(sum(deltas.values()), 2) == 100
    assert deltas[1] == 33.33 and deltas[2] == 33.33 and deltas[3] == 33.34


def test_proporcional_a_meta():
    deltas = ratear(90, [1, 2], "proporcional", {1: 100.0, 2: 200.0})
    assert deltas[1] == 30 and deltas[2] == 60


def test_proporcional_sem_meta_cai_pra_igual():
    deltas = ratear(90, [1, 2, 3], "proporcional", {1: 0, 2: 0, 3: 0})
    assert round(sum(deltas.values()), 2) == 90


def test_manual():
    deltas = ratear(0, [1, 2], "manual", {}, manual={"1": 25, "2": 40})
    assert deltas == {1: 25.0, 2: 40.0}


def test_um_alvo_leva_tudo():
    assert ratear(243, [7], "igual", {}) == {7: 243.0}
