"""Rotas HTML (Jinja2) do painel."""
from flask import Blueprint, render_template, request, redirect, session, url_for

from auth import requer_leitura, login_com_senha
from config import CONFIG

ui = Blueprint("ui", __name__)


@ui.route("/login", methods=["GET", "POST"])
def login():
    erro = None
    if request.method == "POST":
        perfil = login_com_senha(request.form.get("senha", ""))
        if perfil:
            return redirect(request.args.get("next") or url_for("ui.visao_geral"))
        erro = "Senha incorreta."
    return render_template("login.html", erro=erro)


@ui.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("ui.login"))


@ui.route("/")
@ui.route("/painel")
@requer_leitura
def visao_geral():
    return render_template("visao_geral.html", pagina="visao",
                           safra=CONFIG["SAFRA_LABEL"], admin=session.get("auth") == "admin"
                           or not CONFIG["PAINEL_SENHA"])


@ui.route("/consolidado")
@requer_leitura
def consolidado():
    return render_template("consolidado.html", pagina="consolidado",
                           safra=CONFIG["SAFRA_LABEL"], admin=session.get("auth") == "admin"
                           or not CONFIG["PAINEL_SENHA"])


@ui.route("/metas")
@requer_leitura
def metas():
    return render_template("metas.html", pagina="metas",
                           safra=CONFIG["SAFRA_LABEL"], admin=session.get("auth") == "admin"
                           or not CONFIG["PAINEL_SENHA"])


@ui.route("/timeline")
@requer_leitura
def timeline():
    return render_template("timeline.html", pagina="timeline",
                           safra=CONFIG["SAFRA_LABEL"], admin=session.get("auth") == "admin"
                           or not CONFIG["PAINEL_SENHA"])


@ui.route("/config")
@requer_leitura
def config_page():
    return render_template("config.html", pagina="config",
                           safra=CONFIG["SAFRA_LABEL"], admin=session.get("auth") == "admin"
                           or not CONFIG["PAINEL_SENHA"])
