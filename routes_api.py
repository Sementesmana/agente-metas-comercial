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


@api.get("/cultivar/<int:cultivar_id>")
@requer_leitura
def api_cultivar(cultivar_id):
    safra = _safra_req()
    key = f"cultivar:{safra}:{cultivar_id}"
    data = cache.get(key)
    if data is None:
        from metas_service import visao_cultivar
        with db_conn() as conn:
            data = cache.set(key, visao_cultivar(conn, safra, cultivar_id))
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
            if "idscorecard" in p:
                cur.execute("UPDATE safras SET idscorecard=%s WHERE id=%s",
                            ((p["idscorecard"] or "").strip() or None, sid))
            if "label" in p and (p["label"] or "").strip():
                cur.execute("UPDATE safras SET label=%s WHERE id=%s",
                            (p["label"].strip(), sid))
    cache.invalidate()
    return jsonify({"status": "ok"})


# ── De-para SoftExpert (chaves do CPM) ───────────────────────────────────────

def _slug(txt: str) -> str:
    import unicodedata as _u
    import re as _re
    s = _u.normalize("NFD", (txt or "").upper())
    s = "".join(c for c in s if _u.category(c) != "Mn")
    return _re.sub(r"[^A-Z0-9]", "", s)


@api.get("/depara")
@requer_leitura
def api_depara():
    """Todas as combinações da safra (pai por vendedor + vendedor×cultivar de
    metas/realizado) com o de-para atual e sugestão de chave_app."""
    safra = _safra_req()
    with db_conn() as conn, dict_cur(conn) as cur:
        cur.execute("SELECT id, label, sa_safra_id, idscorecard FROM safras WHERE label=%s", (safra,))
        srow = cur.fetchone()
        run = ultimo_run_ok(conn, safra)
        combos = set()
        cur.execute("SELECT DISTINCT vendedor_id, cultivar_id FROM meta_versoes WHERE safra=%s AND vigente", (safra,))
        combos |= {(r["vendedor_id"], r["cultivar_id"]) for r in cur.fetchall()}
        if run:
            cur.execute("""SELECT DISTINCT vendedor_id, cultivar_id FROM snapshot_pedidos
                           WHERE run_id=%s AND incluido AND vendedor_id IS NOT NULL
                             AND cultivar_id IS NOT NULL""", (run["id"],))
            combos |= {(r["vendedor_id"], r["cultivar_id"]) for r in cur.fetchall()}
        combos |= {(v, None) for (v, _c) in combos}  # indicador-pai por vendedor
        cur.execute("SELECT id, nome_sa, nome_exibicao FROM vendedores")
        vends = {r["id"]: r for r in cur.fetchall()}
        cur.execute("SELECT id, nome_norm, nome_exibicao, oculta FROM cultivares")
        cults = {r["id"]: r for r in cur.fetchall()}
        cur.execute("""SELECT vendedor_id, cultivar_id, chave_app, id_indicador,
                              idscmetric, idscorecard
                       FROM depara_se WHERE safra=%s""", (safra,))
        atual = {(r["vendedor_id"], r["cultivar_id"]): r for r in cur.fetchall()}

    sufixo_safra = _slug(safra).replace("SAFRA", "") or "S"
    out = []
    for (v_id, c_id) in combos:
        v = vends.get(v_id)
        if not v:
            continue
        c = cults.get(c_id) if c_id else None
        if c_id and (not c or c.get("oculta")):
            continue
        dp = atual.get((v_id, c_id), {})
        sug = f"COM-{sufixo_safra}-{_slug(v['nome_sa'])[:14]}" + (f"-{_slug(c['nome_norm'])}" if c else "")
        out.append({
            "vendedor_id": v_id, "vendedor": v.get("nome_exibicao") or v["nome_sa"],
            "cultivar_id": c_id, "cultivar": (c.get("nome_exibicao") or c["nome_norm"]) if c else None,
            "chave_app": dp.get("chave_app") or sug,
            "id_indicador": dp.get("id_indicador") or "",
            "idscmetric": dp.get("idscmetric") or "",
        })
    out.sort(key=lambda x: (x["vendedor"], x["cultivar"] is not None, x["cultivar"] or ""))
    total = len(out)
    preenchidos = sum(1 for x in out if x["idscmetric"])
    return jsonify({"status": "ok", "safra": safra,
                    "idscorecard": (srow or {}).get("idscorecard") or "",
                    "safra_id": (srow or {}).get("id"),
                    "resumo": {"total": total, "preenchidos": preenchidos},
                    "data": out})


@api.post("/depara")
@requer_admin
def api_depara_salvar():
    """Upsert de uma linha do de-para (chave: safra × vendedor × cultivar)."""
    p = request.get_json(force=True)
    safra = _safra_req()
    v_id = p.get("vendedor_id")
    c_id = p.get("cultivar_id")  # None = pai
    if not v_id:
        return jsonify({"status": "error", "message": "vendedor_id é obrigatório"}), 400
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO depara_se (safra, vendedor_id, cultivar_id, chave_app,
                                       id_indicador, idscmetric, idscorecard)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (safra, vendedor_id, COALESCE(cultivar_id, 0)) DO UPDATE SET
                    chave_app    = EXCLUDED.chave_app,
                    id_indicador = EXCLUDED.id_indicador,
                    idscmetric   = EXCLUDED.idscmetric,
                    idscorecard  = EXCLUDED.idscorecard
            """, (safra, int(v_id), int(c_id) if c_id else None,
                  (p.get("chave_app") or "").strip() or None,
                  (p.get("id_indicador") or "").strip() or None,
                  (p.get("idscmetric") or "").strip() or None,
                  (p.get("idscorecard") or "").strip() or None))
        from db import audit
        audit(conn, usuario_atual(), "depara_salvo",
              {"safra": safra, "vendedor_id": v_id, "cultivar_id": c_id,
               "idscmetric": p.get("idscmetric"), "id_indicador": p.get("id_indicador")})
    cache.invalidate()
    return jsonify({"status": "ok"})


# ── Carga inicial de metas (planilha do Alex) ────────────────────────────────

@api.post("/metas/importar-seed")
@requer_admin
def api_importar_seed():
    """
    Importa o seed empacotado no repo (seed_metas_safra2627.json) como metas INICIAL.
    SEGURO por padrão: só grava célula que ainda NÃO tem meta vigente — nunca
    sobrescreve manutenção já feita pelo Alex. (sobrescrever=true força.)
    Idempotente: pode clicar de novo sem duplicar nada.
    """
    import json as _json
    import os as _os
    import re as _re

    sobrescrever = bool((request.get_json(silent=True) or {}).get("sobrescrever"))
    caminho = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                            "seed_metas_safra2627.json")
    if not _os.path.exists(caminho):
        return jsonify({"status": "error", "message": "seed_metas_safra2627.json não encontrado"}), 404
    with open(caminho, encoding="utf-8") as f:
        seed = _json.load(f)
    safra = seed["safra"]

    def _norm(s):
        return _re.sub(r"\s+", " ", (s or "").upper().strip())

    resumo = {"gravadas": 0, "puladas_existentes": 0,
              "vendedores_criados": [], "cultivares_criadas": [], "bags_gravadas": 0.0}

    with db_conn() as conn:
        with dict_cur(conn) as cur:
            cur.execute("SELECT id, nome_sa FROM vendedores")
            vmap = {_norm(r["nome_sa"]): r["id"] for r in cur.fetchall()}
            cur.execute("SELECT id, nome_norm FROM cultivares")
            cmap = {r["nome_norm"]: r["id"] for r in cur.fetchall()}

            for item in seed["itens"]:
                vn, cn = _norm(item["vendedor"]), _norm(item["cultivar"])
                if vn not in vmap:   # vendedor da planilha ainda sem venda no SA
                    cur.execute("""INSERT INTO vendedores (nome_sa, nome_exibicao)
                                   VALUES (%s, %s) RETURNING id""",
                                (item["vendedor"], item["vendedor"].title()))
                    vmap[vn] = cur.fetchone()["id"]
                    resumo["vendedores_criados"].append(item["vendedor"])
                if cn not in cmap:
                    cur.execute("""INSERT INTO cultivares (nome_norm, nome_exibicao)
                                   VALUES (%s, %s) RETURNING id""", (cn, cn))
                    cmap[cn] = cur.fetchone()["id"]
                    resumo["cultivares_criadas"].append(cn)

        vigentes = metas_vigentes(conn, safra)
        for item in seed["itens"]:
            v_id = vmap[_norm(item["vendedor"])]
            c_id = cmap[_norm(item["cultivar"])]
            if (v_id, c_id) in vigentes and not sobrescrever:
                resumo["puladas_existentes"] += 1
                continue
            gravar_meta(conn, safra, v_id, c_id, float(item["valor"]), "INICIAL",
                        f"carga inicial — {seed.get('origem', 'planilha')}", usuario_atual())
            resumo["gravadas"] += 1
            resumo["bags_gravadas"] += float(item["valor"])

    cache.invalidate()
    resumo["bags_gravadas"] = round(resumo["bags_gravadas"], 2)
    log.info(f"Importação seed: {resumo}")
    return jsonify({"status": "ok", "safra": safra, "resumo": resumo})
