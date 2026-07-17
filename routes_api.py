"""Endpoints JSON do agente-metas-comercial.

⚡ Leitura passa pelo cache 60s (cache.py); QUALQUER escrita (meta/cadastro)
e o fim de cada sync invalidam tudo — usuário nunca vê dado velho pós-ação.
"""
import logging
import threading

from flask import Blueprint, jsonify, request

import cache
from auth import requer_leitura, requer_admin, usuario_atual
from db import db_conn, dict_cur
from estoque_client import fetch_estoque_map
from metas_service import (
    cascatear, gravar_meta, metas_vigentes, realizado_por_vendedor_cultivar,
    safra_atual, timeline, ultimo_run_ok, visao_consolidado, visao_dashboard,
    visao_vendedor, _cadastros,
)
from pipeline import run_sync

log = logging.getLogger("MetasComercial.API")
api = Blueprint("api", __name__, url_prefix="/api")

_sync_thread = {"t": None}


def _safra_req() -> str:
    """Safra selecionada no painel (?safra=) ou a marcada como atual."""
    return (request.args.get("safra") or "").strip() or safra_atual()


# ── Sync ──────────────────────────────────────────────────────────────────────

@api.post("/sync")
@requer_admin
def api_sync():
    """Botão 'Atualizar agora' — pipeline em background (lock no PG evita paralelo)."""
    t = _sync_thread.get("t")
    if t and t.is_alive():
        return jsonify({"status": "ok", "message": "sincronização já em andamento"}), 202

    def _job():
        try:
            run_sync(force_sa=True)
        except Exception as e:
            log.error(f"sync manual: {e}")

    th = threading.Thread(target=_job, daemon=True)
    th.start()
    _sync_thread["t"] = th
    return jsonify({"status": "ok", "message": "sincronização iniciada"}), 202


@api.get("/sync/status")
@requer_leitura
def api_sync_status():
    # sem cache — é o endpoint de polling do botão
    with db_conn() as conn, dict_cur(conn) as cur:
        cur.execute("""
            SELECT id, inicio::text, fim::text, status, etapa, resumo
            FROM sync_runs ORDER BY id DESC LIMIT 10
        """)
        runs = [dict(r) for r in cur.fetchall()]
    rodando = bool(runs and runs[0]["status"] == "RODANDO")
    return jsonify({"status": "ok", "rodando": rodando, "runs": runs})


# ── Visões (cacheadas 60s) ────────────────────────────────────────────────────

@api.get("/dashboard")
@requer_leitura
def api_dashboard():
    safra = _safra_req()
    data = cache.get(f"dashboard:{safra}")
    if data is None:
        with db_conn() as conn:
            data = cache.set(f"dashboard:{safra}", visao_dashboard(conn, safra))
    return jsonify({"status": "ok", "data": data})


@api.get("/vendedor/<int:vendedor_id>")
@requer_leitura
def api_vendedor(vendedor_id):
    safra = _safra_req()
    key = f"vendedor:{safra}:{vendedor_id}"
    data = cache.get(key)
    if data is None:
        with db_conn() as conn:
            data = cache.set(key, visao_vendedor(conn, safra, vendedor_id))
    return jsonify({"status": "ok", "data": data})


@api.get("/consolidado")
@requer_leitura
def api_consolidado():
    safra = _safra_req()
    payload = cache.get(f"consolidado:{safra}")
    if payload is None:
        estoque_map = fetch_estoque_map()
        with db_conn() as conn:
            data = visao_consolidado(conn, safra, estoque_map)
        payload = cache.set(f"consolidado:{safra}", {"data": data, "estoque_ok": bool(estoque_map)})
    return jsonify({"status": "ok", **payload})


@api.get("/timeline")
@requer_leitura
def api_timeline():
    safra = _safra_req()
    limite = min(int(request.args.get("limite", 200)), 500)
    key = f"timeline:{safra}:{limite}"
    data = cache.get(key)
    if data is None:
        with db_conn() as conn:
            data = cache.set(key, timeline(conn, safra, limite))
    return jsonify({"status": "ok", "data": data})


# ── Metas ─────────────────────────────────────────────────────────────────────

@api.get("/metas/grade")
@requer_leitura
def api_metas_grade():
    safra = _safra_req()
    data = cache.get(f"grade:{safra}")
    if data is None:
        with db_conn() as conn:
            vendedores, cultivares = _cadastros(conn)
            metas = metas_vigentes(conn, safra)
            run = ultimo_run_ok(conn, safra)
            realizado = realizado_por_vendedor_cultivar(conn, run["id"]) if run else {}
        data = cache.set(f"grade:{safra}", {
            "vendedores": [
                {"id": vid, "nome": v.get("nome_exibicao") or v["nome_sa"], "ativo": v["ativo"]}
                for vid, v in vendedores.items()
            ],
            "cultivares": [
                {"id": cid, "nome": c.get("nome_exibicao") or c["nome_norm"], "oculta": c["oculta"]}
                for cid, c in cultivares.items()
            ],
            "metas": [
                {"vendedor_id": v, "cultivar_id": c, "valor": m["valor"]}
                for (v, c), m in metas.items()
            ],
            "realizado": [
                {"vendedor_id": v, "cultivar_id": c, "bags": round(b, 2)}
                for (v, c), b in realizado.items() if v is not None and c is not None
            ],
        })
    return jsonify({"status": "ok", "data": data})


@api.post("/metas")
@requer_admin
def api_gravar_meta():
    p = request.get_json(force=True)
    campos = ("vendedor_id", "cultivar_id", "valor")
    if any(p.get(k) is None for k in campos):
        return jsonify({"status": "error", "message": f"campos obrigatórios: {campos}"}), 400
    with db_conn() as conn:
        vid = gravar_meta(
            conn, _safra_req(), int(p["vendedor_id"]), int(p["cultivar_id"]),
            float(p["valor"]), p.get("tipo", "AJUSTE"),
            (p.get("motivo") or "").strip() or "edição manual", usuario_atual(),
        )
    cache.invalidate()
    return jsonify({"status": "ok", "versao_id": vid})


@api.post("/metas/cascata/preview")
@requer_admin
def api_cascata_preview():
    return _cascata(preview=True)


@api.post("/metas/cascata")
@requer_admin
def api_cascata():
    return _cascata(preview=False)


def _cascata(preview: bool):
    p = request.get_json(force=True)
    cultivar_id = p.get("cultivar_id")
    volume = p.get("volume")
    alvos = [int(x) for x in (p.get("vendedor_ids") or [])]
    modo = p.get("modo", "igual")
    motivo = (p.get("motivo") or "").strip()
    if not cultivar_id or volume is None or not alvos:
        return jsonify({"status": "error",
                        "message": "cultivar_id, volume e vendedor_ids são obrigatórios"}), 400
    if not preview and not motivo:
        return jsonify({"status": "error", "message": "motivo é obrigatório pra aplicar"}), 400
    with db_conn() as conn:
        plano = cascatear(conn, _safra_req(), int(cultivar_id), float(volume),
                          alvos, modo, motivo, usuario_atual(),
                          manual=p.get("manual"), preview=preview)
    if not preview:
        cache.invalidate()
    return jsonify({"status": "ok", "preview": preview, "plano": plano})


@api.get("/metas/historico")
@requer_leitura
def api_metas_historico():
    vend = request.args.get("vendedor_id")
    cult = request.args.get("cultivar_id")
    sql = """
        SELECT mv.id, mv.vendedor_id, mv.cultivar_id, mv.valor_bags::float AS valor,
               mv.tipo_evento, mv.motivo, mv.criado_por, mv.criado_em::text, mv.vigente,
               v.nome_sa AS vendedor, c.nome_exibicao AS cultivar
        FROM meta_versoes mv
        JOIN vendedores v ON v.id = mv.vendedor_id
        JOIN cultivares c ON c.id = mv.cultivar_id
        WHERE mv.safra = %s
    """
    params = [_safra_req()]
    if vend:
        sql += " AND mv.vendedor_id=%s"
        params.append(int(vend))
    if cult:
        sql += " AND mv.cultivar_id=%s"
        params.append(int(cult))
    sql += " ORDER BY mv.criado_em DESC LIMIT 500"
    with db_conn() as conn, dict_cur(conn) as cur:
        cur.execute(sql, params)
        return jsonify({"status": "ok", "data": [dict(r) for r in cur.fetchall()]})


# ── Config (vendedores / cultivares) ─────────────────────────────────────────

@api.get("/vendedores")
@requer_leitura
def api_vendedores():
    with db_conn() as conn, dict_cur(conn) as cur:
        cur.execute("""
            SELECT id, nome_sa, nome_exibicao, data_contratacao::text, ativo
            FROM vendedores ORDER BY nome_sa
        """)
        return jsonify({"status": "ok", "data": [dict(r) for r in cur.fetchall()]})


@api.post("/vendedores/<int:vid>")
@requer_admin
def api_vendedor_update(vid):
    p = request.get_json(force=True)
    sets, params = [], []
    for campo in ("nome_exibicao", "data_contratacao", "ativo"):
        if campo in p:
            sets.append(f"{campo}=%s")
            params.append(p[campo] or None if campo == "data_contratacao" else p[campo])
    if not sets:
        return jsonify({"status": "error", "message": "nada pra atualizar"}), 400
    params.append(vid)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE vendedores SET {', '.join(sets)} WHERE id=%s", params)
    cache.invalidate()
    return jsonify({"status": "ok"})


@api.post("/cultivares/<int:cid>")
@requer_admin
def api_cultivar_update(cid):
    p = request.get_json(force=True)
    sets, params = [], []
    for campo in ("nome_exibicao", "oculta"):
        if campo in p:
            sets.append(f"{campo}=%s")
            params.append(p[campo])
    if not sets:
        return jsonify({"status": "error", "message": "nada pra atualizar"}), 400
    params.append(cid)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE cultivares SET {', '.join(sets)} WHERE id=%s", params)
    cache.invalidate()
    return jsonify({"status": "ok"})


# ── Safras (cadastro/seletor) ─────────────────────────────────────────────────

@api.get("/safras")
@requer_leitura
def api_safras():
    with db_conn() as conn, dict_cur(conn) as cur:
        cur.execute("SELECT id, label, sa_safra_id, atual FROM safras ORDER BY label DESC")
        return jsonify({"status": "ok", "data": [dict(r) for r in cur.fetchall()],
                        "atual": safra_atual()})


@api.post("/safras")
@requer_admin
def api_safra_criar():
    p = request.get_json(force=True)
    label = (p.get("label") or "").strip()
    sa_id = (p.get("sa_safra_id") or "").strip()
    if not label:
        return jsonify({"status": "error", "message": "label é obrigatório"}), 400
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO safras (label, sa_safra_id, atual) VALUES (%s, %s, FALSE)
                ON CONFLICT (label) DO UPDATE SET sa_safra_id = EXCLUDED.sa_safra_id
                RETURNING id
            """, (label, sa_id or None))
            sid = cur.fetchone()[0]
    cache.invalidate()
    return jsonify({"status": "ok", "id": sid})


@api.post("/safras/<int:sid>")
@requer_admin
def api_safra_update(sid):
    p = request.get_json(force=True)
    with db_conn() as conn:
        with conn.cursor() as cur:
            if p.get("atual"):
                # só UMA safra atual — o sync/cron passa a rodar pra ela
                cur.execute("UPDATE safras SET atual=FALSE WHERE atual")
                cur.execute("UPDATE safras SET atual=TRUE WHERE id=%s", (sid,))
            if "sa_safra_id" in p:
                cur.execute("UPDATE safras SET sa_safra_id=%s WHERE id=%s",
                            ((p["sa_safra_id"] or "").strip() or None, sid))
            if "label" in p and (p["label"] or "").strip():
                cur.execute("UPDATE safras SET label=%s WHERE id=%s",
                            (p["label"].strip(), sid))
    cache.invalidate()
    return jsonify({"status": "ok"})
