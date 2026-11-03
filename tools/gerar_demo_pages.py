"""Gera as páginas de demonstração do GitHub Pages (pasta docs/) com dados fictícios.

Os painéis reais (tools/dashboard.py e tools/monitor_compras.py) são apps Flask que leem o Pipefy e o SAP.
Aqui o mesmo HTML é renderizado pelo próprio Flask, mas com dados inventados no lugar das APIs:
- painel de lançamentos: o relatório do Pipefy é trocado por um DataFrame fictício;
- monitor de compras: a rota /api/dados é respondida no navegador por um shim de fetch,
  com pedidos fictícios gerados relativos à data de hoje.

Uso:
    pip install flask pandas requests openpyxl
    python tools/gerar_demo_pages.py
"""
import json
import os
import random
import sys
import types
from datetime import datetime, timedelta

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(RAIZ, "tools"), os.path.join(RAIZ, "src")]
if "slack_sdk" not in sys.modules:  # o monitor importa o cliente do Slack só para o relatório semanal
    sk = types.ModuleType("slack_sdk"); wh = types.ModuleType("slack_sdk.webhook")
    wh.WebhookClient = object; sk.webhook = wh
    sys.modules.update({"slack_sdk": sk, "slack_sdk.webhook": wh})

# config.py exige estas variáveis; a demonstração nunca chama o SAP nem o Pipefy
for _v in ["SAP_PASSWORD", "PIPEFY_TOKEN", "WEBHOOK_SECRET", "BASIC_AUTH_PASSWORD", "BASIC_AUTH_USERNAME"]:
    os.environ.setdefault(_v, "demo")

import pandas as pd  # noqa: E402

DOCS = os.path.join(RAIZ, "docs")
REPO = "https://github.com/Joao-2003/sap-pipefy-invoice-automation"
rnd = random.Random(2026)

FORNECEDORES = ["Alfa Componentes Ltda", "Beta Aços S.A.", "Gama Usinagem Ltda", "Delta Embalagens Ltda",
                "Épsilon Elétrica Ltda", "Zeta Ferramentas ME", "Eta Hidráulica Ltda", "Teta Logística S.A.",
                "Iota Plásticos Ltda", "Kappa Tintas Ltda", "Lambda Rolamentos Ltda", "Mi Serviços Técnicos ME"]
COMPRADORES = [("CB100001", "Ana Souza"), ("CB100002", "Bruno Martins"), ("CB100003", "Carla Nunes"),
               ("CB100004", "Diego Alves"), ("CB100005", "Elisa Rocha"), ("CB100006", "Fábio Lima"),
               ("CB100007", "Gabriela Reis"), ("CB100008", "Henrique Dias")]

BANNER = f"""
<div style="position:fixed;left:0;right:0;bottom:0;z-index:9999;background:#f5a623;color:#0c1810;
 font:600 12px/1.6 'JetBrains Mono',monospace;text-align:center;padding:4px 12px">
 Demonstração com dados fictícios · <a href="{REPO}" style="color:#0c1810">código no GitHub</a> ·
 <a href="index.html" style="color:#0c1810">outros painéis</a></div>
<script>window.baixarXlsx = function (e) {{ if (e) e.preventDefault();
 alert('Na versão real este botão exporta o recorte filtrado para Excel. Indisponível na demonstração.'); }};</script>
"""

# (texto do erro como a automação grava no Pipefy, peso)
ERROS = [
    ("XML da NF não encontrado no card", 0),  # tratado à parte (cards sem XML)
    ("Pedido de compra 45000{po} ainda não está liberado para recebimento", 6),
    ("Bloqueio de filial: CNPJ da NF não pertence ao centro do pedido", 3),
    ("Não há mais itens disponíveis no pedido para a quantidade da nota", 8),
    ("Divergência de NCM no item 10 (uso e consumo)", 5),
    ("Divergência de preço: valor unitário da nota acima do pedido", 7),
    ("Entrada de mercadorias (MIGO) não encontrada para o pedido", 9),
    ("Unidade de medida MH sem regras para converter na unidade do pedido", 2),
    ("Tax code ausente no item do pedido", 3),
    ("Condições de pagamento divergentes entre nota e pedido", 4),
    ("Períodos contábeis fechados para a data de lançamento", 1),
    ("Nota fiscal já criada com esta referência (impossível lançar)", 3),
]


def erro_aleatorio():
    textos, pesos = zip(*ERROS[1:])
    return rnd.choices(textos, pesos)[0].replace("{po}", str(rnd.randint(10000, 99999)))


def cards_ficticios(inicio=datetime(2026, 7, 8), fim=datetime(2026, 10, 31), n=1400):
    linhas, dias = [], (fim - inicio).days
    for i in range(n):
        entrada = inicio + timedelta(days=rnd.random() * dias, hours=rnd.randint(7, 18))
        nf = f"{rnd.randint(1000, 99999):06d}"
        forn = rnd.choice(FORNECEDORES)
        servico = rnd.random() < 0.12
        sorteio = rnd.random()
        sem_xml = (not servico) and sorteio < 0.07
        sucesso = (not sem_xml) and sorteio < 0.95
        erro_final = "" if sucesso else ("XML da NF não encontrado no card" if sem_xml else erro_aleatorio())
        # alguns cards lançados passaram por erros antes (histórico)
        hist, t = [], entrada
        for _ in range(rnd.choice([0, 0, 0, 1, 1, 2]) if sucesso else rnd.choice([1, 1, 2, 3])):
            t += timedelta(hours=rnd.randint(2, 30))
            hist.append(f"{t:%d/%m/%Y %H:%M} | {erro_aleatorio()} [NF {nf}]")
        if not sucesso and erro_final:
            hist.append(f"{t + timedelta(hours=1):%d/%m/%Y %H:%M} | {erro_final} [NF {nf}]")
        fase = ("Lançadas" if rnd.random() < 0.6 else "Pagamento") if sucesso else rnd.choice(
            ["Notas Fiscais com Pedido", "Notas Fiscais com Pedido", "Pendentes"])
        linhas.append({
            "Código": 900000000 + i,
            "Título": f"NF {nf} · {forn}",
            "Fase atual": fase,
            "Nº Documento SAP": f"51{rnd.randint(10000000, 99999999)}" if sucesso else "",
            "Detalhes do Erro": erro_final,
            "Etiquetas": "MATERIA_PRIMA" if rnd.random() < 0.3 else "",
            "Tipo de solicitação": "Serviços" if servico else "Compra de materiais",
            "Qual o tipo de documento fiscal?": "Serviço NFS" if servico else "NF-e",
            "Card validado?": "Sim" if rnd.random() < 0.8 else "Não",
            "Entrou na fase Notas Fiscais com Pedido": entrada.strftime("%Y-%m-%d %H:%M"),
            "Histórico de Erros": "\n".join(hist),
            "Auditoria Fiscal": "✅ Contas a Pagar: parcelas ajustadas ao vencimento da nota"
            if sucesso and rnd.random() < 0.18 else "",
        })
    return pd.DataFrame(linhas)


def pedidos_ficticios(n=1600, dias=180):
    import monitor_compras as mc
    termos = list(mc.MAPA_CONDICOES) + ["VAZIA / SEM CONDIÇÃO"]
    favoritas = {"C005": 60, "C004": 45, "0045": 35, "C001": 28, "V060": 22, "0001": 18, "VAZIA / SEM CONDIÇÃO": 9}
    pesos = [favoritas.get(t, rnd.choice([0, 0, 1, 1, 2])) for t in termos]
    pedidos = []
    for i in range(n):
        sup = rnd.randrange(len(FORNECEDORES))
        uid, nome = rnd.choice(COMPRADORES)
        cond = rnd.choices(termos, pesos)[0]
        pedidos.append({
            "PurchaseOrder": f"45{rnd.randint(10000000, 99999999)}",
            "DiasAtras": rnd.randint(0, dias),
            "PurchasingGroup": rnd.choices(["G01", "G02", "G03", "G04"], [5, 4, 3, 2])[0],
            "SupplierID": str(100200 + sup), "SupplierName": FORNECEDORES[sup],
            "PaymentTerms": cond, "Condicao_Exibicao": mc.obter_nome_condicao(cond),
            "CreatedByUser": uid, "UserName": nome,
            "ValorTotal": round(rnd.lognormvariate(8.3, 1.2), 2),
        })
    return pedidos


SHIM_MONITOR = """
<script>
// Demonstração: responde a rota /api/dados no navegador, com a mesma agregação do api_dados() do Flask.
(function () {
  const PEDIDOS = __PEDIDOS__;
  const hoje = new Date();
  const iso = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  PEDIDOS.forEach((p) => { p.CreationDate = iso(new Date(hoje.getTime() - p.DiasAtras * 864e5)); });
  const agrupa = (lista, chaves) => {
    const m = new Map();
    for (const p of lista) {
      const k = chaves.map((c) => p[c]).join('|');
      const g = m.get(k) || Object.fromEntries(chaves.map((c) => [c, p[c]]).concat([['Qtd', 0], ['Valor', 0]]));
      g.Qtd += 1; g.Valor += p.ValorTotal; m.set(k, g);
    }
    return [...m.values()].sort((a, b) => b.Valor - a.Valor);
  };
  const real = window.fetch.bind(window);
  window.fetch = async (url, opts) => {
    if (!String(url).startsWith('api/dados')) return real(url, opts);
    const q = new URLSearchParams(String(url).split('?')[1]);
    const start = q.get('start'), end = q.get('end'), group = q.get('group') || '';
    const sel = PEDIDOS.filter((p) => p.CreationDate >= start && p.CreationDate <= end &&
      (!group || (group === 'NOT_G04' ? p.PurchasingGroup !== 'G04' : p.PurchasingGroup === group)));
    const total = sel.length, valor_total = sel.reduce((s, p) => s + p.ValorTotal, 0);
    const porCond = new Map();
    sel.forEach((p) => { const k = p.PaymentTerms; (porCond.get(k) || porCond.set(k, []).get(k)).push(p); });
    const resumo = [...porCond.values()].map((g) => ({
      PaymentTerms: g[0].PaymentTerms, Condicao_Exibicao: g[0].Condicao_Exibicao, Quantidade: g.length,
      Percentual: total ? Math.round((g.length / total) * 1000) / 10 : 0,
      ValorTotal: g.reduce((s, p) => s + p.ValorTotal, 0),
      AllUsers: agrupa(g, ['CreatedByUser', 'UserName']), AllSuppliers: agrupa(g, ['SupplierID', 'SupplierName']),
    })).sort((a, b) => b.Quantidade - a.Quantidade);
    return new Response(JSON.stringify({ resumo, total, valor_total }), { headers: { 'Content-Type': 'application/json' } });
  };
})();
</script>
"""

INDEX = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Automação de notas fiscais · demonstração</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
body{margin:0;background:#0c1810;color:#dff0e4;font:14px/1.6 'JetBrains Mono',monospace}
main{max-width:880px;margin:0 auto;padding:48px 20px}
h1{font:600 30px 'Playfair Display',Georgia,serif;margin:0 0 8px}
p{color:#8aab94}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;margin-top:28px}
a.card{display:block;background:#141f17;border:1px solid #253828;border-radius:12px;padding:20px;color:inherit;text-decoration:none}
a.card:hover{border-color:#3ecf6e}
a.card b{color:#3ecf6e;font-size:16px}
img{width:100%;border-radius:8px;margin-top:12px;border:1px solid #1d2f20}
</style></head><body><main>
<h1>Automação de notas fiscais · SAP + Pipefy</h1>
<p>Demonstração dos painéis do projeto com <b>dados fictícios</b>. Na versão real, os dados vêm ao vivo do Pipefy e do SAP S/4HANA.
Código e documentação: <a href="REPO" style="color:#5ba4cf">REPO</a></p>
<div class="grid">
<a class="card" href="painel-lancamentos.html"><b>Painel de lançamentos</b><br>Taxa de sucesso da automação, motivos de falha por área responsável, histórico de erros e cards pendentes.<img src="painel_indicadores.png" alt=""></a>
<a class="card" href="monitor-compras.html"><b>Monitor de compras</b><br>Pedidos do SAP por condição de pagamento, com drill-down por comprador e fornecedor.</a>
</div></main></body></html>
"""


def main():
    import dashboard
    df = cards_ficticios()
    dashboard.baixar_relatorio_pipefy = lambda *a, **k: df.copy()
    html = dashboard.app.test_client().get("/").get_data(as_text=True)
    html = html.replace("</body>", BANNER + "</body>").replace("<title>Automação Fiscal</title>",
                                                               "<title>Painel de lançamentos · demonstração</title>")
    open(os.path.join(DOCS, "painel-lancamentos.html"), "w", encoding="utf-8").write(html)

    import monitor_compras
    html = monitor_compras.app.test_client().get("/").get_data(as_text=True)
    shim = SHIM_MONITOR.replace("__PEDIDOS__", json.dumps(pedidos_ficticios(), ensure_ascii=False))
    html = html.replace("<head>", "<head>" + shim, 1)
    html = html.replace("document.getElementById('dtStart').value = hoje;",
                        "document.getElementById('dtStart').value = new Date(Date.now() - 30 * 864e5).toISOString().split('T')[0];")
    html = html.replace("</body>", BANNER + "</body>").replace("<title>Monitor de Compras</title>",
                                                               "<title>Monitor de compras · demonstração</title>")
    open(os.path.join(DOCS, "monitor-compras.html"), "w", encoding="utf-8").write(html)

    open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8").write(INDEX.replace("REPO", REPO))
    open(os.path.join(DOCS, ".nojekyll"), "w").close()
    print("ok: docs/index.html, docs/painel-lancamentos.html, docs/monitor-compras.html")


if __name__ == "__main__":
    main()
