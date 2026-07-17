"""
Autenticação do painel — padrão da casa:
- PAINEL_SENHA (leitura) e ADMIN_SENHA (edição de metas/config; default = PAINEL_SENHA).
- Sessão via cookie assinado do Flask.
- Suporta ?senha= na URL (padrão de embed em Página WEB do SoftExpert,
  onde não há tela de login — ver reference_embed_painel_se).
"""
from functools import wraps

from flask import request, session, redirect, url_for, jsonify

from config import CONFIG, admin_senha


def _tentar_login_por_query():
    senha = request.args.get("senha")
    if not senha:
        return
    if admin_senha() and senha == admin_senha():
        session["auth"] = "admin"
    elif CONFIG["PAINEL_SENHA"] and senha == CONFIG["PAINEL_SENHA"]:
        session["auth"] = "leitura"


def login_com_senha(senha: str) -> str | None:
    if admin_senha() and senha == admin_senha():
        session["auth"] = "admin"
        return "admin"
    if CONFIG["PAINEL_SENHA"] and senha == CONFIG["PAINEL_SENHA"]:
        session["auth"] = "leitura"
        return "leitura"
    return None


def requer_leitura(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not CONFIG["PAINEL_SENHA"]:          # sem senha configurada → aberto (dev)
            return f(*args, **kwargs)
        if not session.get("auth"):
            _tentar_login_por_query()
        if session.get("auth"):
            return f(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"status": "error", "message": "não autenticado"}), 401
        return redirect(url_for("ui.login", next=request.path))
    return wrapper


def requer_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not CONFIG["PAINEL_SENHA"]:
            return f(*args, **kwargs)
        if not session.get("auth"):
            _tentar_login_por_query()
        if session.get("auth") == "admin":
            return f(*args, **kwargs)
        return jsonify({"status": "error", "message": "requer perfil admin"}), 403
    return wrapper


def usuario_atual() -> str:
    return "admin" if session.get("auth") == "admin" else "leitura"
