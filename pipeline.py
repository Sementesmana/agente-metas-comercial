"""
Pipeline sequencial atômico:  (1) ingestão SA → (2) snapshot → (3) diff/eventos.
(Fase 2 acrescenta a etapa 4: push SoftExpert.)

- Advisory lock do Postgres impede execução concorrente (cron × botão).
- Se a etapa 1 falhar, nada é gravado — o último snapshot íntegro permanece.
- Diff em nível pedido×cultivar → eventos NOVO / AJUSTE / SAIU_FUNIL /
  ENTROU_FUNIL / REMOVIDO (o "por que mudou" do Alex).
"""
import json
import logging

import psycopg2.extras

from db import get_db
from sa_client import fetch_linhas_sa, agregar_pedido_cultivar

log = logging.getLogger("MetasComercial.Pipeline")

LOCK_KEY = 742601  # advisory lock exclusivo deste agente


# ── Diff puro (testável sem banco) ────────────────────────────────────────────

def diff_snapshots(old: dict, new: dict) -> list:
    """
    old/new: {(numpedido, cultivar_norm): row} com row['bags'] e row['incluido'].
    Retorna lista de eventos:
      {tipo, numpedido, cultivar_norm, vendedor, delta_bags, detalhe}
    """
    eventos = []
    for key, n in new.items():
        o = old.get(key)
        if o is None:
            if n["incluido"]:
                eventos.append(_ev("NOVO", key, n, n["bags"],
                                   f"Pedido novo · {n['cliente']} · status {n['status_raw']}"))
            continue
        if o["incluido"] and not n["incluido"]:
            eventos.append(_ev("SAIU_FUNIL", key, n, -o["bags"],
                               f"Status mudou p/ '{n['status_raw']}' — sai do vendido"))
        elif not o["incluido"] and n["incluido"]:
            eventos.append(_ev("ENTROU_FUNIL", key, n, n["bags"],
                               f"Status mudou p/ '{n['status_raw']}' — entra no vendido"))
        elif n["incluido"] and abs(n["bags"] - o["bags"]) > 1e-9:
            delta = n["bags"] - o["bags"]
            eventos.append(_ev("AJUSTE", key, n, delta,
                               f"Quantidade {o['bags']:g} → {n['bags']:g} bags"))
    for key, o in old.items():
        if key not in new and o["incluido"]:
            eventos.append(_ev("REMOVIDO", key, o, -o["bags"],
                               "Pedido não aparece mais no SA (excluído/cancelado)"))
    return eventos


def _ev(tipo, key, row, delta, detalhe):
    return {
        "tipo": tipo,
        "numpedido": key[0],
        "cultivar_norm": key[1],
        "vendedor": row.get("vendedor", ""),
        "delta_bags": round(delta, 4),
        "detalhe": detalhe,
    }


# ── Execução ──────────────────────────────────────────────────────────────────

def run_sync(force_sa: bool = True) -> dict:
    """Roda o pipeline completo. Retorna resumo. Lança exceção se lock ocupado."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))
            if not cur.fetchone()[0]:
                raise RuntimeError("Sincronização já em andamento (lock ocupado).")
        try:
            return _run_sync_locked(conn, force_sa)
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    finally:
        conn.close()


def _run_sync_locked(conn, force_sa: bool) -> dict:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO sync_runs (status, etapa) VALUES ('RODANDO','INGESTAO_SA') RETURNING id")
        run_id = cur.fetchone()[0]
    conn.commit()

    try:
        # ── Etapa 1: ingestão SA (funil-espelho do agente-estoque) ──
        linhas = fetch_linhas_sa(force=force_sa)
        if not linhas:
            raise RuntimeError("SA devolveu 0 linhas — abortando sem gravar snapshot.")
        novo = agregar_pedido_cultivar(linhas)

        # ── Etapa 2: cadastro incremental + snapshot ──
        _set_etapa(conn, run_id, "SNAPSHOT")
        vend_ids = _upsert_vendedores(conn, {r["vendedor"] for r in novo.values() if r["vendedor"]})
        cult_ids = _upsert_cultivares(conn, {(r["cultivar_norm"], r["cultivar_nome"]) for r in novo.values()})

        rows = [
            (run_id, r["numpedido"], r["data"] or None, r["cliente"],
             r["vendedor"], vend_ids.get(r["vendedor"]),
             r["cultivar_norm"], cult_ids.get(r["cultivar_norm"]),
             round(r["bags"], 4), r["status_raw"], r["incluido"],
             r["uso_semente"], r["filial"])
            for r in novo.values()
        ]
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO snapshot_pedidos
                    (run_id, numpedido, data_pedido, cliente, vendedor_nome, vendedor_id,
                     cultivar_norm, cultivar_id, bags, status_raw, incluido, uso_semente, filial)
                VALUES %s
            """, rows)

        # ── Etapa 3: diff vs último run OK ──
        _set_etapa(conn, run_id, "DIFF")
        antigo = _load_snapshot_anterior(conn, run_id)
        eventos = diff_snapshots(antigo, novo) if antigo else []
        if eventos:
            ev_rows = [
                (run_id, e["tipo"], e["numpedido"],
                 vend_ids.get(e["vendedor"]),
                 cult_ids.get(e["cultivar_norm"]),
                 e["delta_bags"], e["detalhe"])
                for e in eventos
            ]
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, """
                    INSERT INTO eventos
                        (run_id, tipo, numpedido, vendedor_id, cultivar_id, delta_bags, detalhe)
                    VALUES %s
                """, ev_rows)

        vendido = round(sum(r["bags"] for r in novo.values() if r["incluido"]), 2)
        resumo = {
            "linhas": len(novo), "vendido_bags": vendido,
            "eventos": len(eventos), "primeiro_run": not bool(antigo),
        }
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sync_runs SET status='OK', etapa=NULL, fim=NOW(), resumo=%s WHERE id=%s",
                (json.dumps(resumo), run_id),
            )
        conn.commit()
        log.info(f"Sync #{run_id} OK: {resumo}")
        return {"run_id": run_id, **resumo}

    except Exception as e:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sync_runs SET status='ERRO', fim=NOW(), resumo=%s WHERE id=%s",
                (json.dumps({"erro": str(e)[:500]}), run_id),
            )
        conn.commit()
        log.error(f"Sync #{run_id} ERRO: {e}")
        raise


def _set_etapa(conn, run_id, etapa):
    with conn.cursor() as cur:
        cur.execute("UPDATE sync_runs SET etapa=%s WHERE id=%s", (etapa, run_id))


def _upsert_vendedores(conn, nomes: set) -> dict:
    ids = {}
    with conn.cursor() as cur:
        for nome in sorted(nomes):
            cur.execute("""
                INSERT INTO vendedores (nome_sa, nome_exibicao) VALUES (%s, %s)
                ON CONFLICT (nome_sa) DO UPDATE SET nome_sa = EXCLUDED.nome_sa
                RETURNING id
            """, (nome, nome.title()))
            ids[nome] = cur.fetchone()[0]
    return ids


def _upsert_cultivares(conn, pares: set) -> dict:
    ids = {}
    with conn.cursor() as cur:
        for norm, nome in sorted(pares):
            cur.execute("""
                INSERT INTO cultivares (nome_norm, nome_exibicao) VALUES (%s, %s)
                ON CONFLICT (nome_norm) DO UPDATE SET nome_norm = EXCLUDED.nome_norm
                RETURNING id
            """, (norm, nome))
            ids[norm] = cur.fetchone()[0]
    return ids


def _load_snapshot_anterior(conn, run_atual: int):
    """Snapshot do último run OK anterior, como dict {(numpedido, cultivar_norm): row}."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id FROM sync_runs WHERE status='OK' AND id < %s ORDER BY id DESC LIMIT 1",
            (run_atual,),
        )
        prev = cur.fetchone()
        if not prev:
            return None
        cur.execute("""
            SELECT numpedido, cultivar_norm, vendedor_nome AS vendedor, cliente,
                   bags::float AS bags, status_raw, incluido
            FROM snapshot_pedidos WHERE run_id = %s
        """, (prev["id"],))
        return {(r["numpedido"], r["cultivar_norm"]): dict(r) for r in cur.fetchall()}
