# agente-metas-comercial — "CPM Comercial" · Sementes Maná

App de gestão de metas da área comercial (gestor: Alex). Triângulo:
**Simple Agro** (realizado, automático) → **APP** (fonte da verdade da meta:
edição, cascateamento, versionamento) → **SoftExpert CPM/Desempenho** (Fase 2).

## Regra de ouro

O **vendido** aqui é o MESMO NÚMERO do agente-estoque: mesma leitura do SA
(`item.quantidade` já em bags), mesmos status (`aguardando aprovacao`,
`integrado`, `aprovado`), mesma safra/grupo (env vars com os MESMOS valores).
Se o funil mudar no agente-estoque, mudar aqui junto (`sa_client.py` + `config.py`).

## Arquitetura

```
sa_client.py       → espelho do modelo SA do agente-estoque (login, /api/orders, funil)
pipeline.py        → (1) ingestão SA → (2) snapshot → (3) diff/eventos  [advisory lock]
metas_service.py   → metas versionadas, cascateamento, visões do painel
estoque_client.py  → GET /api/estoque do agente-estoque (estoque inicial; só leitura)
scheduler.py       → cron diário 08h BRT (APScheduler)
routes_api.py      → /api/* JSON  ·  routes_ui.py → páginas Jinja2
db.py              → banco-mana, schema `metas` (bootstrap idempotente)
```

Telas: Visão geral (KPIs + ranking + drill do vendedor) · Consolidado por cultivar
(estoque × meta × vendido) · Gestão de metas (grade editável + cascata com preview
e motivo obrigatório) · Linha do tempo (por que mudou) · Config (contratação,
cultivares ocultas, histórico de sync).

Toda edição de meta cria **versão nova** (`meta_versoes`, vigente=true única por
safra×vendedor×cultivar) — histórico completo pra próxima safra. Cada sync grava
**snapshot por pedido×cultivar** e o **diff** vira eventos
(NOVO / AJUSTE / SAIU_FUNIL / ENTROU_FUNIL / REMOVIDO).

## Deploy (Railway)

1. Repo GitHub: `Sementesmana/agente-metas-comercial` (git push → auto-deploy).
2. Projeto Railway novo apontando pro repo (railway.toml já configura o start).
3. Env vars (ver `.env.example`): `DATABASE_URL` do **banco-mana** (schema `metas`
   é criado sozinho), `SA_USERNAME/SA_PASSWORD` + `SA_SAFRA_ID/SA_GRUPO_ID`
   **com os mesmos valores do agente-estoque**, `PAINEL_SENHA`, `ADMIN_SENHA`,
   `SECRET_KEY`.
4. Primeiro acesso: botão **⟳ Atualizar agora** popula vendedores/cultivares e o
   primeiro snapshot. Depois, carga inicial de metas na tela Gestão de metas
   (ou cascata por cultivar).

Auth: `PAINEL_SENHA` = leitura, `ADMIN_SENHA` = edição. Suporta `?senha=` na URL
(padrão de embed em Página WEB do SoftExpert).

## Fase 2 (SE CPM) — plugável

- Tabela `depara_se` já existe (vendedor/cultivar → IDSCMETRIC; ids determinísticos
  no padrão `COM-<VENDEDOR>-<CULTIVAR>`).
- Estrutura via planilhas STR* (DI014) · valores via SOAP
  `addMultipleMeasuresInAdinterface` · config: indicador Mensal, acumulação Soma
  na janela da safra, meta por período de acumulação, faixa do acumulado.
- Docs: `ORQUESTRADOR/Softexpert CPM/`.

## Testes

```
python -m pytest tests/   # funil espelho, diff, rateio (17 testes)
```
