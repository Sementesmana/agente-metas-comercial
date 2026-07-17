"""
Configuração do agente-metas-comercial.
Credenciais SEMPRE via env var (Railway) — nunca hardcode.

⚠️ FUNIL-ESPELHO DO AGENTE-ESTOQUE:
Os valores de SA_SAFRA_ID / SA_GRUPO_ID e o conjunto STATUS_INCLUIDOS devem ser
IDÊNTICOS aos do agente-estoque, senão o realizado diverge do painel de estoque.
Se o funil mudar lá, mudar aqui junto (regra registrada na nota do vault).
"""
import os

CONFIG = {
    # ── Simple Agro (mesmos valores/defaults do agente-estoque) ──
    "SA_BASE_URL":   os.getenv("SA_BASE_URL",  "https://sementesmana.api.simpleagro.com.br:3333"),
    "SA_USERNAME":   os.getenv("SA_USERNAME",  ""),
    "SA_PASSWORD":   os.getenv("SA_PASSWORD",  ""),
    "SAFRA_ID":      os.getenv("SA_SAFRA_ID",  "69a5d85cae03f50036ee2531"),
    "GRUPO_SOJA_ID": os.getenv("SA_GRUPO_ID",  "610a8b743829fd00385c48c9"),

    # ── Banco (banco-mana, schema próprio) ──
    "DATABASE_URL":  os.getenv("DATABASE_URL", ""),
    "DB_SCHEMA":     os.getenv("DB_SCHEMA", "metas"),

    # ── agente-estoque (leitura do estoque inicial por cultivar) ──
    "ESTOQUE_API_URL": os.getenv(
        "ESTOQUE_API_URL",
        "https://agente-estoque-sa-production.up.railway.app"
    ),

    # ── App ──
    "SAFRA_LABEL":  os.getenv("SAFRA_LABEL", "SAFRA 26"),
    "PAINEL_SENHA": os.getenv("PAINEL_SENHA", ""),          # leitura
    "ADMIN_SENHA":  os.getenv("ADMIN_SENHA", ""),           # edição de metas/config (default: PAINEL_SENHA)
    "SECRET_KEY":   os.getenv("SECRET_KEY", "troque-em-producao"),
    "CRON_HOUR":    int(os.getenv("CRON_HOUR", "8")),       # pipeline diário 08h BRT
    "TZ":           os.getenv("TZ", "America/Sao_Paulo"),
}

# Status SA que contam como VENDIDO (espelho exato do agente-estoque)
STATUS_INCLUIDOS = {"aguardando aprovacao", "integrado", "aprovado"}

# Cache de pedidos SA (mesmo TTL do agente-estoque)
SA_CACHE_TTL = 1800  # 30 min

# Cache da leitura do /api/estoque do agente-estoque
ESTOQUE_CACHE_TTL = 600  # 10 min


def admin_senha() -> str:
    return CONFIG["ADMIN_SENHA"] or CONFIG["PAINEL_SENHA"]
