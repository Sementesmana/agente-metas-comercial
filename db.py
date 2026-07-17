"""
Banco: banco-mana (PostgreSQL), schema próprio `metas` (padrão schema-por-agente).
Bootstrap idempotente. Disciplina de roundtrips: agregações no SQL, não em loop.
"""
import logging

import psycopg2
import psycopg2.extras

from config import CONFIG

log = logging.getLogger("MetasComercial.DB")

SCHEMA = CONFIG["DB_SCHEMA"]


def get_db():
    if not CONFIG["DATABASE_URL"]:
        raise RuntimeError("DATABASE_URL não configurada!")
    conn = psycopg2.connect(CONFIG["DATABASE_URL"])
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}, public")
    return conn


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
    status  TEXT DEFAULT 'RODANDO',        -- RODANDO | OK | ERRO
    etapa   TEXT,                          -- etapa em execução / que falhou
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
    tipo        TEXT NOT NULL,   -- NOVO | AJUSTE | SAIU_FUNIL | ENTROU_FUNIL | REMOVIDO
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
    cultivar_id INT REFERENCES cultivares(id),   -- NULL = indicador-pai do vendedor
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
"""


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
    log.info(f"Schema '{SCHEMA}' pronto.")


def dict_cur(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def audit(conn, usuario: str, acao: str, payload: dict):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_log (usuario, acao, payload) VALUES (%s, %s, %s)",
            (usuario, acao, psycopg2.extras.Json(payload)),
        )
