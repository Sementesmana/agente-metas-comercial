"""
agente-metas-comercial — "CPM Comercial" · Sementes Maná LTDA
=============================================================
App de gestão de metas da área comercial (gestor: Alex).

Triângulo: Simple Agro (realizado) → APP (fonte da verdade da meta) → SoftExpert CPM.
Fase 1: ingestão SA (funil-espelho do agente-estoque) + painel + metas versionadas.
Fase 2 (plugável): gerador de planilhas STR* + push SOAP addMultipleMeasuresInAdinterface.

Regra de ouro: o VENDIDO aqui é O MESMO NÚMERO do agente-estoque
(mesmos status, mesma leitura de item.quantidade em bags, mesma safra/grupo).
"""
import logging
import os

from flask import Flask, jsonify

from config import CONFIG

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("MetasComercial")

app = Flask(__name__)
app.secret_key = CONFIG["SECRET_KEY"]

from routes_api import api  # noqa: E402
from routes_ui import ui    # noqa: E402

app.register_blueprint(api)
app.register_blueprint(ui)


@app.route("/health")
def health():
    from datetime import datetime
    deps = {"db": False}
    try:
        from db import get_db
        with get_db() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            deps["db"] = True
    except Exception:
        pass
    status = "ok" if deps["db"] else "degraded"
    return jsonify({"status": status, "deps": deps, "ts": datetime.now().isoformat()})


def _bootstrap():
    """Init DB + scheduler (uma vez por processo; Procfile usa 1 worker gthread)."""
    try:
        from db import init_db
        init_db()
    except Exception as e:
        log.error(f"init_db falhou (app sobe mesmo assim): {e}")
    if os.getenv("DISABLE_CRON") != "1":
        try:
            from scheduler import start_scheduler
            start_scheduler()
        except Exception as e:
            log.error(f"scheduler falhou: {e}")


_bootstrap()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)), debug=True)
