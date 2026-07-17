"""
Banco: banco-mana (PostgreSQL remoto), schema próprio `metas`.

⚡ PERF (receita 2026-07-11 — banco remoto exige disciplina de roundtrips):
- POOL de conexões (ThreadedConnectionPool 1..8, casa com as 8 threads do gunicorn)
  com keepalives — sem handshake TCP/TLS por request.
- search_path definido via `options` na CRIAÇÃO da conexão (zero roundtrip extra).
- Rotas usam `with db_conn() as conn:` (commit/rollback + devolve ao pool).
- Pipeline (job longo) usa get_db()/put_db() explícitos.
"""
import logging
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool

from config import CONFIG

log = logging.getLogger("MetasComercial.DB")

SCHEMA = CONFIG["DB_SCHEMA"]

_pool = None
_pool_lock = threading.Lock()


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                if not CONFIG["DATABASE_URL"]:
                    raise RuntimeError("DATABASE_URL não configurada!")
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1, maxconn=8,
                    dsn=CONFIG["DATABASE_URL"],
                    options=f"-c search_path={SCHEMA},public",
                    keepalives=1, keepalives_idle=30,
                    keepalives_interval=10, keepalives_count=3,
                )
                log.info("Pool de conexões criado (1..8, keepalive).")
    return _pool


def get_db():
    """Pega conexão do pool (uso longo/explícito — devolver com put_db)."""
    conn = _get_pool().getconn()
    if conn.closed:            # conexão morta devolvida ao pool — troca
        _get_pool().putconn(conn, close=True)
        conn = _get_pool().getconn()
    return conn


def put_db(conn):
    """Devolve conexão ao pool (fecha se estiver quebrada)."""
    try:
        _get_pool().putconn(conn, close=bool(conn.closed))
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


@contextmanager
def db_conn():
    """Uso padrão nas rotas: commit no sucesso, rollback no erro, devolve ao pool."""
    conn = get_db()
    try:
        yield conn
        if not conn.closed:
            conn.commit()
    except Exception:
        if not conn.closed:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        put_db(conn)


DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
SET search_path TO {SCHEMA}, public;

CREATE TABLE IF NOT EXISTS vendedores (
    id               SERIAL PRIMARY KEY,
    nome_sa          TEXT UNIQUE NOT NULL,
    nome_exibicao    TEXT,
    data_contratacao DATE,
    ativo            BOOLEAN DEFAULT TRUE,
    criado_em        TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cultivares (
    id            SERIAL PRIMARY KEY,
    nome_norm     TEXT UNIQUE NOT NULL,
    nome_exibicao TEXT,
    oculta        BOOLEAN DEFAULT FALSE,
    criado_em     TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS meta_versoes (
    id          SERIAL PRIMARY KEY,
    safra       TEXT NOT NULL,
    vendedor_id INT NOT NULL REFERENCES vendedores(id),
    cultivar_id INT NOT NULL REFERENCES cultivares(id),
    valor_bags  NUMERIC(14,2) NOT NULL DEFAULT 0,
    tipo_evento TEXT NOT NULL DEFAULT 'AJUSTE',
    motivo      TEXT,
    criado_por  TEXT DEFAULT 'admin',
    criado_em   TIMESTAMP DEFAULT NOW(),
    vigente     BOOLEAN DEFAULT TRUE
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_meta_vigente
    ON meta_versoes (safra, vendedor_id, cultivar_id) WHERE vigente;
CREATE INDEX IF NOT EXISTS ix_meta_hist
    ON meta_versoes (safra, vendedor_id, cultivar_id, criado_em);

CREATE TABLE IF NOT EXISTS sync_runs (
    id      SERIAL PRIMARY KEY,
    inicio  TIMESTAMP DEFAULT NOW(),
    fim     TIMESTAMP,
    status  TEXT DEFAULT 'RODANDO',
    etapa   TEXT,
    resumo  JSONB DEFAULT '{{}}'::jsonb
);

CREATE TABLE IF NOT EXISTS snapshot_pedidos (
    id            SERIAL PRIMARY KEY,
    run_id        INT NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    numpedido     TEXT NOT NULL,
    data_pedido   DATE,
    cliente       TEXT,
    vendedor_nome TEXT,
    vendedor_id   INT REFERENCES vendedores(id),
    cultivar_norm TEXT NOT NULL,
    cultivar_id   INT REFERENCES cultivares(id),
    bags          NUMERIC(14,4) NOT NULL DEFAULT 0,
    status_raw    TEXT,
    incluido      BOOLEAN DEFAULT FALSE,
    uso_semente   TEXT,
    filial        TEXT
);
CREATE INDEX IF NOT EXISTS ix_snap_run ON snapshot_pedidos (run_id);
CREATE INDEX IF NOT EXISTS ix_snap_key ON snapshot_pedidos (run_id, numpedido, cultivar_norm);

CREATE TABLE IF NOT EXISTS eventos (
    id          SERIAL PRIMARY KEY,
    run_id      INT REFERENCES sync_runs(id) ON DELETE CASCADE,
    tipo        TEXT NOT NULL,
    numpedido   TEXT,
    vendedor_id INT REFERENCES vendedores(id),
    cultivar_id INT REFERENCES cultivares(id),
    delta_bags  NUMERIC(14,4) DEFAULT 0,
    detalhe     TEXT,
    criado_em   TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_eventos_ts ON eventos (criado_em DESC);

CREATE TABLE IF NOT EXISTS depara_se (
    id          SERIAL PRIMARY KEY,
    vendedor_id INT NOT NULL REFERENCES vendedores(id),
    cultivar_id INT REFERENCES cultivares(id),
    idscmetric  TEXT,
    idscorecard TEXT,
    UNIQUE (vendedor_id, cultivar_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id        SERIAL PRIMARY KEY,
    usuario   TEXT,
    acao      TEXT,
    payload   JSONB,
    criado_em TIMESTAMP DEFAULT NOW()
);

-- Multi-safra (2026-07-17): cadastro de safras; metas/sync/visões amarradas por label
CREATE TABLE IF NOT EXISTS safras (
    id          SERIAL PRIMARY KEY,
    label       TEXT UNIQUE NOT NULL,
    sa_safra_id TEXT,
    atual       BOOLEAN DEFAULT FALSE,
    criado_em   TIMESTAMP DEFAULT NOW()
);
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS safra TEXT;

-- De-para SE por safra. Modelo de DOIS NÍVEIS do SoftExpert (explicação Xayer 2026-07-17):
--   1) id_indicador  = código do CADASTRO do indicador (ST007) — a variedade XYZ é
--      UM indicador de catálogo, compartilhado pelos N vendedores;
--   2) idscmetric    = identificador da AMARRAÇÃO no scorecard — a XYZ debaixo de
--      cada vendedor ganha um ID próprio. É ESTA a chave que o
--      addMeasuresInAdinterface usa pra gravar meta/medição.
-- Chave lógica: safra × vendedor × cultivar (cultivar NULL = indicador-pai do vendedor)
ALTER TABLE depara_se ADD COLUMN IF NOT EXISTS safra TEXT;
ALTER TABLE depara_se ADD COLUMN IF NOT EXISTS chave_app TEXT;      -- ex. COM-2627-JOSE-761I2X
ALTER TABLE depara_se ADD COLUMN IF NOT EXISTS id_indicador TEXT;   -- código do cadastro (nível 1)
ALTER TABLE depara_se DROP CONSTRAINT IF EXISTS depara_se_vendedor_id_cultivar_id_key;
CREATE UNIQUE INDEX IF NOT EXISTS ux_depara_safra
    ON depara_se (safra, vendedor_id, COALESCE(cultivar_id, 0));
"""


def init_db():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            # Seed/migração idempotente da safra corrente
            cur.execute("SELECT COUNT(*) FROM safras")
            if cur.fetchone()[0] == 0:
                cur.execute(
                    "INSERT INTO safras (label, sa_safra_id, atual) VALUES (%s, %s, TRUE)",
                    (CONFIG["SAFRA_LABEL"], CONFIG["SAFRA_ID"]),
                )
                # Renomeia registros antigos do rótulo provisório "SAFRA 26"
                cur.execute("UPDATE meta_versoes SET safra=%s WHERE safra='SAFRA 26'",
                            (CONFIG["SAFRA_LABEL"],))
            cur.execute("SELECT label FROM safras WHERE atual LIMIT 1")
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE sync_runs SET safra=%s WHERE safra IS NULL", (row[0],))
    log.info(f"Schema '{SCHEMA}' pronto.")


def safra_atual_db() -> dict:
    """Safra marcada como atual (fallback: env)."""
    try:
        with db_conn() as conn, dict_cur(conn) as cur:
            cur.execute("SELECT id, label, sa_safra_id FROM safras WHERE atual LIMIT 1")
            row = cur.fetchone()
            if row:
                return dict(row)
    except Exception:
        pass
    return {"id": None, "label": CONFIG["SAFRA_LABEL"], "sa_safra_id": CONFIG["SAFRA_ID"]}


def dict_cur(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def audit(conn, usuario: str, acao: str, payload: dict):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_log (usuario, acao, payload) VALUES (%s, %s, %s)",
            (usuario, acao, psycopg2.extras.Json(payload)),
        )
