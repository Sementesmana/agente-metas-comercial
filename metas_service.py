"""
Serviço de metas: versionamento, cascateamento e visões agregadas.

Regras de negócio (Alex):
- Meta vigente por safra × vendedor × cultivar; TODA edição cria versão nova
  (histórico completo → aprendizado da próxima safra).
- Cascateamento SOMA volume por cima das metas vigentes (encalhado dividido).
- Duas análises: meta global do vendedor (soma) × meta por cultivar.
- Superação >100% fica registrada — realocação não apaga histórico.
"""
import logging

from db import get_db, dict_cur, audit
from config import CONFIG

log = logging.getLogger("MetasComercial.Metas")

TIPOS = {"INICIAL", "CASCATA", "REALOCACAO", "AJUSTE", "VOLUME_NOVO"}


# ── Realizado (a partir do último snapshot OK) ────────────────────────────────

def ultimo_run_ok(conn):
    with dict_cur(conn) as cur:
        cur.execute("SELECT id, fim, resumo FROM sync_runs WHERE status='OK' ORDER BY id DESC LIMIT 1")
        return cur.fetchone()


def realizado_por_vendedor_cultivar(conn, run_id: int) -> dict:
    """{(vendedor_id, cultivar_id): bags} apenas incluído no funil."""
    with dict_cur(conn) as cur:
        cur.execute("""
            SELECT vendedor_id, cultivar_id, SUM(bags)::float AS bags
            FROM snapshot_pedidos
            WHERE run_id=%s AND incluido
            GROUP BY vendedor_id, cultivar_id
        """, (run_id,))
        return {(r["vendedor_id"], r["cultivar_id"]): r["bags"] for r in cur.fetchall()}


# ── Metas vigentes ────────────────────────────────────────────────────────────

def metas_vigentes(conn, safra: str) -> dict:
    """{(vendedor_id, cultivar_id): {valor, versao_id}}"""
    with dict_cur(conn) as cur:
        cur.execute("""
            SELECT id, vendedor_id, cultivar_id, valor_bags::float AS valor
            FROM meta_versoes WHERE safra=%s AND vigente
        """, (safra,))
        return {(r["vendedor_id"], r["cultivar_id"]): {"valor": r["valor"], "versao_id": r["id"]}
                for r in cur.fetchall()}


def gravar_meta(conn, safra: str, vendedor_id: int, cultivar_id: int, valor: float,
                tipo: str, motivo: str, usuario: str):
    """Cria versão nova (desativa a vigente). Idempotente se valor não mudou."""
    tipo = tipo if tipo in TIPOS else "AJUSTE"
    with dict_cur(conn) as cur:
        cur.execute("""
            SELECT id, valor_bags::float AS valor FROM meta_versoes
            WHERE safra=%s AND vendedor_id=%s AND cultivar_id=%s AND vigente
        """, (safra, vendedor_id, cultivar_id))
        atual = cur.fetchone()
        if atual and abs(atual["valor"] - valor) < 1e-9:
            return atual["id"]  # nada mudou
        if atual:
            cur.execute("UPDATE meta_versoes SET vigente=FALSE WHERE id=%s", (atual["id"],))
        cur.execute("""
            INSERT INTO meta_versoes
                (safra, vendedor_id, cultivar_id, valor_bags, tipo_evento, motivo, criado_por, vigente)
            VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE) RETURNING id
        """, (safra, vendedor_id, cultivar_id, round(valor, 2), tipo, motivo, usuario))
        novo_id = cur.fetchone()["id"]
    audit(conn, usuario, "meta_gravada", {
        "safra": safra, "vendedor_id": vendedor_id, "cultivar_id": cultivar_id,
        "de": atual["valor"] if atual else None, "para": valor, "tipo": tipo, "motivo": motivo,
    })
    return novo_id


# ── Cascateamento ─────────────────────────────────────────────────────────────

def ratear(volume: float, alvos: list, modo: str, metas_atuais: dict, manual: dict | None = None) -> dict:
    """
    Divide `volume` entre vendedores `alvos` (ids).
    modo: 'igual' | 'proporcional' (à meta vigente da cultivar; fallback igual) | 'manual'
    Retorna {vendedor_id: delta_bags}. Ajusta arredondamento na última posição.
    """
    if not alvos:
        return {}
    if modo == "manual":
        return {int(k): float(v) for k, v in (manual or {}).items() if float(v) != 0}
    if modo == "proporcional":
        base = {v: metas_atuais.get(v, 0.0) for v in alvos}
        total = sum(base.values())
        if total > 0:
            deltas = {v: round(volume * base[v] / total, 2) for v in alvos}
        else:
            deltas = {v: round(volume / len(alvos), 2) for v in alvos}
    else:  # igual
        deltas = {v: round(volume / len(alvos), 2) for v in alvos}
    # corrige resíduo de arredondamento no último
    residuo = round(volume - sum(deltas.values()), 2)
    if abs(residuo) >= 0.01:
        ultimo = alvos[-1]
        deltas[ultimo] = round(deltas[ultimo] + residuo, 2)
    return deltas


def cascatear(conn, safra: str, cultivar_id: int, volume: float, alvos: list,
              modo: str, motivo: str, usuario: str, manual: dict | None = None,
              preview: bool = False) -> list:
    """Aplica (ou simula) cascateamento: SOMA delta na meta vigente de cada alvo."""
    vigentes = metas_vigentes(conn, safra)
    metas_cult = {v: vigentes.get((v, cultivar_id), {}).get("valor", 0.0) for v in alvos}
    deltas = ratear(volume, alvos, modo, metas_cult, manual)
    plano = []
    for vend_id, delta in deltas.items():
        antes = metas_cult.get(vend_id, 0.0)
        depois = round(antes + delta, 2)
        plano.append({"vendedor_id": vend_id, "antes": antes, "delta": delta, "depois": depois})
        if not preview:
            gravar_meta(conn, safra, vend_id, cultivar_id, depois, "CASCATA", motivo, usuario)
    if not preview:
        audit(conn, usuario, "cascata", {
            "safra": safra, "cultivar_id": cultivar_id, "volume": volume,
            "modo": modo, "motivo": motivo, "plano": plano,
        })
    return plano


# ── Visões agregadas (payloads do painel) ─────────────────────────────────────

def _cadastros(conn):
    with dict_cur(conn) as cur:
        cur.execute("SELECT * FROM vendedores ORDER BY nome_sa")
        vendedores = {r["id"]: dict(r) for r in cur.fetchall()}
        cur.execute("SELECT * FROM cultivares ORDER BY nome_exibicao")
        cultivares = {r["id"]: dict(r) for r in cur.fetchall()}
    return vendedores, cultivares


def visao_dashboard(conn, safra: str) -> dict:
    run = ultimo_run_ok(conn)
    vendedores, cultivares = _cadastros(conn)
    metas = metas_vigentes(conn, safra)
    realizado = realizado_por_vendedor_cultivar(conn, run["id"]) if run else {}

    por_vend = {}
    for (v_id, c_id), m in metas.items():
        por_vend.setdefault(v_id, {"meta": 0.0, "vendido": 0.0})
        por_vend[v_id]["meta"] += m["valor"]
    for (v_id, c_id), bags in realizado.items():
        if v_id is None:
            continue
        por_vend.setdefault(v_id, {"meta": 0.0, "vendido": 0.0})
        por_vend[v_id]["vendido"] += bags

    ranking = []
    for v_id, agg in por_vend.items():
        v = vendedores.get(v_id, {})
        if v and v.get("ativo") is False:
            continue
        meta, vendido = round(agg["meta"], 2), round(agg["vendido"], 2)
        ranking.append({
            "vendedor_id": v_id,
            "nome": v.get("nome_exibicao") or v.get("nome_sa") or "?",
            "data_contratacao": str(v.get("data_contratacao") or "") or None,
            "meta": meta, "vendido": vendido,
            "falta": round(max(meta - vendido, 0), 2),
            "pct": round(vendido / meta, 4) if meta else None,
        })
    ranking.sort(key=lambda x: (x["pct"] is None, -(x["pct"] or 0)))

    meta_total = round(sum(r["meta"] for r in ranking), 2)
    vendido_total = round(sum(r["vendido"] for r in ranking), 2)
    return {
        "safra": safra,
        "ultimo_sync": str(run["fim"]) if run else None,
        "kpis": {
            "meta_total": meta_total,
            "vendido_total": vendido_total,
            "pct_geral": round(vendido_total / meta_total, 4) if meta_total else None,
        },
        "ranking": ranking,
    }


def visao_vendedor(conn, safra: str, vendedor_id: int) -> dict:
    run = ultimo_run_ok(conn)
    vendedores, cultivares = _cadastros(conn)
    metas = metas_vigentes(conn, safra)
    realizado = realizado_por_vendedor_cultivar(conn, run["id"]) if run else {}

    cult_ids = ({c for (v, c) in metas if v == vendedor_id} |
                {c for (v, c) in realizado if v == vendedor_id and c is not None})
    linhas = []
    for c_id in cult_ids:
        c = cultivares.get(c_id, {})
        if c.get("oculta"):
            continue
        meta = metas.get((vendedor_id, c_id), {}).get("valor", 0.0)
        vendido = realizado.get((vendedor_id, c_id), 0.0)
        linhas.append({
            "cultivar_id": c_id,
            "cultivar": c.get("nome_exibicao") or c.get("nome_norm") or "?",
            "meta": round(meta, 2), "vendido": round(vendido, 2),
            "falta": round(meta - vendido, 2),
            "pct": round(vendido / meta, 4) if meta else None,
        })
    linhas.sort(key=lambda x: -x["vendido"])

    pedidos = []
    if run:
        with dict_cur(conn) as cur:
            cur.execute("""
                SELECT numpedido, data_pedido::text AS data, cliente, cultivar_norm,
                       bags::float AS bags, status_raw, incluido, uso_semente
                FROM snapshot_pedidos
                WHERE run_id=%s AND vendedor_id=%s
                ORDER BY data_pedido DESC NULLS LAST
            """, (run["id"], vendedor_id))
            pedidos = [dict(r) for r in cur.fetchall()]

    v = vendedores.get(vendedor_id, {})
    return {
        "vendedor": {
            "id": vendedor_id,
            "nome": v.get("nome_exibicao") or v.get("nome_sa"),
            "data_contratacao": str(v.get("data_contratacao") or "") or None,
        },
        "linhas": linhas,
        "totais": {
            "meta": round(sum(l["meta"] for l in linhas), 2),
            "vendido": round(sum(l["vendido"] for l in linhas), 2),
        },
        "pedidos": pedidos,
    }


def visao_consolidado(conn, safra: str, estoque_map: dict) -> list:
    """
    estoque_map: {cultivar_norm: {producao_bag, compra_bag, qualidade_bag}}
    (vindo do /api/estoque do agente-estoque — fonte do estoque inicial).
    """
    run = ultimo_run_ok(conn)
    vendedores, cultivares = _cadastros(conn)
    metas = metas_vigentes(conn, safra)
    realizado = realizado_por_vendedor_cultivar(conn, run["id"]) if run else {}

    por_cult = {}
    for (v_id, c_id), m in metas.items():
        por_cult.setdefault(c_id, {"meta": 0.0, "vendido": 0.0})
        por_cult[c_id]["meta"] += m["valor"]
    for (v_id, c_id), bags in realizado.items():
        if c_id is None:
            continue
        por_cult.setdefault(c_id, {"meta": 0.0, "vendido": 0.0})
        por_cult[c_id]["vendido"] += bags

    out = []
    for c_id, agg in por_cult.items():
        c = cultivares.get(c_id, {})
        if c.get("oculta"):
            continue
        norm = c.get("nome_norm", "")
        est = estoque_map.get(norm, {})
        disponivel = round(est.get("producao_bag", 0.0) + est.get("compra_bag", 0.0)
                           - est.get("qualidade_bag", 0.0), 2)
        meta, vendido = round(agg["meta"], 2), round(agg["vendido"], 2)
        out.append({
            "cultivar_id": c_id,
            "cultivar": c.get("nome_exibicao") or norm,
            "estoque_inicial": disponivel,
            "meta": meta, "vendido": vendido,
            "saldo_disponivel": round(disponivel - vendido, 2),
            "saldo_apos_meta": round(disponivel - meta, 2),
            "pct": round(vendido / meta, 4) if meta else None,
            "sem_estoque_info": norm not in estoque_map,
        })
    out.sort(key=lambda x: x["cultivar"])
    return out


def timeline(conn, safra: str, limite: int = 200) -> list:
    """Eventos do realizado + versões de meta num feed único."""
    vendedores, cultivares = _cadastros(conn)
    feed = []
    with dict_cur(conn) as cur:
        cur.execute("""
            SELECT tipo, numpedido, vendedor_id, cultivar_id,
                   delta_bags::float AS delta, detalhe, criado_em
            FROM eventos ORDER BY criado_em DESC, id DESC LIMIT %s
        """, (limite,))
        for r in cur.fetchall():
            feed.append({
                "origem": "REALIZADO", "tipo": r["tipo"], "quando": str(r["criado_em"]),
                "vendedor": _nome_v(vendedores, r["vendedor_id"]),
                "cultivar": _nome_c(cultivares, r["cultivar_id"]),
                "delta": r["delta"], "detalhe": f"Pedido {r['numpedido']} · {r['detalhe']}",
            })
        cur.execute("""
            SELECT tipo_evento, vendedor_id, cultivar_id, valor_bags::float AS valor,
                   motivo, criado_por, criado_em
            FROM meta_versoes WHERE safra=%s
            ORDER BY criado_em DESC, id DESC LIMIT %s
        """, (safra, limite))
        for r in cur.fetchall():
            feed.append({
                "origem": "META", "tipo": r["tipo_evento"], "quando": str(r["criado_em"]),
                "vendedor": _nome_v(vendedores, r["vendedor_id"]),
                "cultivar": _nome_c(cultivares, r["cultivar_id"]),
                "delta": r["valor"],
                "detalhe": f"Meta → {r['valor']:g} bags · {r['motivo'] or 'sem motivo'} · por {r['criado_por']}",
            })
    feed.sort(key=lambda x: x["quando"], reverse=True)
    return feed[:limite]


def _nome_v(vendedores, vid):
    v = vendedores.get(vid, {})
    return v.get("nome_exibicao") or v.get("nome_sa") or "—"


def _nome_c(cultivares, cid):
    c = cultivares.get(cid, {})
    return c.get("nome_exibicao") or c.get("nome_norm") or "—"


def safra_atual() -> str:
    return CONFIG["SAFRA_LABEL"]
