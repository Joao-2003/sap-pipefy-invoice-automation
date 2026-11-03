"""
relatorio_conta_contabil.py -- levantamento (roda uma vez) de CONTA CONTÁBIL x MATERIAL x UTILIZAÇÃO
nas compras com pedido, para descobrir se já existe padrão suficiente para virar trava
(na automação ou no SAP) antes do lançamento.

Fontes (nenhuma CDS precisou ser editada):
  1. API_SUPPLIERINVOICE_PROCESS_SRV / A_SupplierInvoice  -> faturas do ano (data, fornecedor,
     status, estorno) + o nº interno da NF (to_BR_SupplierInvoiceNFDocument)
  2. YY1_NF_ITEM_TAX_CDS -> por item da NF: utilização, NCM, material, centro, centro de custo,
     CONTA DO RAZÃO (conta de destino do item), CFOP, código de imposto
     (a CDS traz uma linha por imposto; o script junta em uma linha por item)
  3. API_PRODUCT_SRV / A_Product -> tipo de material (FERT, HALB, HAWA, ROH, VERP...) e grupo
     + A_ProductDescription -> descrição do material
  4. Pedido de cada item (A_SuplrInvcItemPurOrdRef da fatura) + texto do item no pedido
     (A_PurchaseOrderItem.PurchaseOrderItemText) -- ajuda nos materiais genéricos (NLAG)

Abas "top-down" (13/10/2026): partem da CONTA e descem para utilização, material, pedido.

Uso (na pasta do nf_automation, com o mesmo .env da automação):
  python relatorio_conta_contabil.py                       # 2025 e 2026 completos
  python relatorio_conta_contabil.py --anos 2026 --limite 100   # teste rápido (100 faturas)
  python relatorio_conta_contabil.py --plano-contas "lançamentos_2026.xlsx"   # nomes das contas (aba 'cr')

O progresso fica em relatorio_conta_cache.json: se cair no meio, rodar de novo continua de onde parou
(use --sem-cache para começar do zero).
"""
import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

import requests
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import sap_client as sc

BASE_PROD = sc.SAP_BASE_URL.rstrip("/") + "/sap/opu/odata/sap/API_PRODUCT_SRV"
BASE_PO = sc.SAP_BASE_URL.rstrip("/") + "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV"
CACHE = "relatorio_conta_cache.json"

USOS = {
    "0": "0 - Revenda",
    "1": "1 - Industrialização / Imob. industrial",
    "2": "2 - Consumo geral / Demais operações",
    "3": "3 - Imobilizado",
    "4": "4 - Consumo atividade principal / industrial",
}
TIPOS = {"FERT": "Produto acabado", "HALB": "Semiacabado", "HAWA": "Revendável",
         "ROH": "Matéria-prima", "VERP": "Embalagem"}

# Classificação das combinações (para discussão de regra)
MIN_REGRA = 3          # ocorrências mínimas para chamar de "regra candidata"
PCT_QUASE = 0.90       # conta dominante >= 90% -> "quase regra"
MIN_QUASE = 5

_s = requests.Session()


def log(msg):
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Acesso ao SAP
# ─────────────────────────────────────────────────────────────────────────────
def get(url, params=None):
    """GET OData com retentativa. Devolve o conteúdo de 'd'."""
    for tentativa in range(5):
        try:
            r = _s.get(url, params=params, headers={**sc._auth_headers(), "Accept": "application/json"},
                       **sc._auth_kwargs(), timeout=90)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            return r.json().get("d", {})
        except requests.RequestException as e:
            resp = getattr(e, "response", None)
            if resp is not None and resp.status_code in (400, 401, 403, 404):
                raise
            if tentativa == 4:
                raise
            time.sleep(3 * (tentativa + 1))


def paginar(url, params, top, limite=None):
    skip = 0
    while True:
        p = {**params, "$top": str(top), "$skip": str(skip), "$format": "json"}
        res = get(url, p).get("results", [])
        for r in res:
            yield r
        skip += len(res)
        if len(res) < top or (limite and skip >= limite):
            return


def _data(v):
    if not v:
        return ""
    s = str(v)
    if "/Date(" in s:
        import re
        ms = int(re.search(r"-?\d+", s).group())
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")
    return s[:10]


# ─────────────────────────────────────────────────────────────────────────────
#  1. Faturas do ano + nº interno da NF
# ─────────────────────────────────────────────────────────────────────────────
def listar_faturas(anos, empresa, limite):
    campos = ("SupplierInvoice,FiscalYear,PostingDate,InvoicingParty,SupplierInvoiceStatus,"
              "IsReversal,IsReversed,SupplierInvoiceIsCreditMemo")
    faturas = []
    for ano in anos:
        base = {"$filter": f"FiscalYear eq '{ano}' and CompanyCode eq '{empresa}'",
                "$expand": "to_BR_SupplierInvoiceNFDocument,to_SuplrInvcItemPurOrdRef"}
        url = f"{sc.BASE}/A_SupplierInvoice"
        try:
            linhas = list(paginar(url, {**base, "$select": campos + ",to_BR_SupplierInvoiceNFDocument,"
                                                                  "to_SuplrInvcItemPurOrdRef"}, 500, limite))
        except requests.HTTPError as e:
            log(f"   $select com expand recusado ({e}); tentando sem $select...")
            linhas = list(paginar(url, base, 500, limite))
        for r in linhas:
            nfd = r.get("to_BR_SupplierInvoiceNFDocument") or {}
            if isinstance(nfd, dict) and "results" in nfd:
                nfd = (nfd["results"] or [{}])[0]
            nf = str(nfd.get("BR_NotaFiscal") or "").strip()
            refs = r.get("to_SuplrInvcItemPurOrdRef") or {}
            refs = refs.get("results", []) if isinstance(refs, dict) else (refs or [])
            pos = [[str(x.get("SupplierInvoiceItem", "")).lstrip("0"), str(x.get("PurchaseOrder", "")).strip(),
                    str(x.get("PurchaseOrderItem", "")).lstrip("0")] for x in refs if x.get("PurchaseOrder")]
            faturas.append({
                "fatura": r.get("SupplierInvoice"), "ano": r.get("FiscalYear"),
                "data": _data(r.get("PostingDate")), "fornecedor": r.get("InvoicingParty", ""),
                "status": str(r.get("SupplierInvoiceStatus", "")),
                "estornada": bool(r.get("IsReversal")) or bool(r.get("IsReversed")),
                "credito": bool(r.get("SupplierInvoiceIsCreditMemo")),
                "nf": nf.zfill(10) if nf else "",
                "pos": pos,
            })
        log(f"   {ano}: {len(linhas)} fatura(s)")
    return faturas


# ─────────────────────────────────────────────────────────────────────────────
#  2. Itens da NF (CDS de impostos) -> uma linha por item
# ─────────────────────────────────────────────────────────────────────────────
CAMPOS_NF = ("BR_NotaFiscal,BR_NotaFiscalItem,BR_TaxType,BR_CFOPCode,BR_MaterialUsage,NCMCode,Material,"
             "Plant,CostCenter,GLAccount,BR_NFItemBaseAmount,BR_TaxCode")


def ler_itens_nf(nfs, itens, lote=20):
    pendentes = [n for n in nfs if n not in itens["_lidas"]]
    log(f"2/3 Itens das NFs: {len(pendentes)} NF(s) a ler ({len(nfs) - len(pendentes)} já no cache)")
    for i in range(0, len(pendentes), lote):
        grupo = pendentes[i:i + lote]
        filtro = " or ".join(f"BR_NotaFiscal eq '{n}'" for n in grupo)
        for r in paginar(f"{sc.BASE_CDS_TAX}/YY1_NF_ITEM_TAX_CDS", {"$filter": filtro, "$select": CAMPOS_NF}, 5000):
            chave = f"{r['BR_NotaFiscal']}|{r['BR_NotaFiscalItem']}"
            it = itens["dados"].setdefault(chave, {
                "nf": r["BR_NotaFiscal"], "item": r["BR_NotaFiscalItem"], "cfop": r.get("BR_CFOPCode", ""),
                "uso": str(r.get("BR_MaterialUsage", "")).strip(), "ncm": r.get("NCMCode", ""),
                "material": str(r.get("Material", "")).strip(), "centro": r.get("Plant", ""),
                "cc": r.get("CostCenter", ""), "conta": str(r.get("GLAccount", "")).strip(),
                "cod_imposto": r.get("BR_TaxCode", ""), "bases": {}})
            try:
                it["bases"][r.get("BR_TaxType", "")] = float(r.get("BR_NFItemBaseAmount") or 0)
            except ValueError:
                pass
        itens["_lidas"].extend(grupo)
        if (i // lote) % 10 == 0:
            log(f"   {min(i + lote, len(pendentes))}/{len(pendentes)} NF(s)")
            salvar_cache()
    return itens


def valor_item(bases):
    """Valor aproximado do item: base do CBS/IBS (2026 = valor líquido do item); senão base de PIS/COFINS;
    senão a maior base. É só para ponderar volume -- a contagem de itens é a métrica principal."""
    for t in ("CBS1", "IB1S", "IPIS", "ICOF"):
        if bases.get(t):
            return bases[t]
    return max(bases.values()) if bases else 0.0


# ─────────────────────────────────────────────────────────────────────────────
#  3. Tipo de material
# ─────────────────────────────────────────────────────────────────────────────
def ler_tipos_material(materiais, produtos):
    pend = [m for m in materiais if m and m not in produtos]
    log(f"3/3 Tipo de material: {len(pend)} material(is) a consultar")
    falhas_auth = 0
    for i, m in enumerate(pend, 1):
        try:
            d = get(f"{BASE_PROD}/A_Product('{m}')", {"$select": "Product,ProductType,ProductGroup", "$format": "json"})
            produtos[m] = {"tipo": d.get("ProductType", ""), "grupo": d.get("ProductGroup", "")}
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", None)
            produtos[m] = {"tipo": "?", "grupo": ""}
            if code in (401, 403):
                falhas_auth += 1
                if falhas_auth >= 5:
                    log("   Sem acesso à API de produto: tipo de material fica '?'.")
                    for x in pend[i:]:
                        produtos.setdefault(x, {"tipo": "?", "grupo": ""})
                    break
        if i % 200 == 0:
            log(f"   {i}/{len(pend)}")
            salvar_cache()
    return produtos


def ler_descricoes(materiais, descricoes, lote=25):
    """Descrição do material (A_ProductDescription), em português; sem PT, a primeira que houver."""
    pend = [m for m in materiais if m and m not in descricoes]
    log(f"   Descrição de material: {len(pend)} a consultar")
    for i in range(0, len(pend), lote):
        grupo = pend[i:i + lote]
        filtro = " or ".join(f"Product eq '{m}'" for m in grupo)
        achadas = {}
        try:
            for r in paginar(f"{BASE_PROD}/A_ProductDescription", {"$filter": f"({filtro})",
                                                                   "$select": "Product,Language,ProductDescription"}, 500):
                m, lang = str(r.get("Product", "")).strip(), str(r.get("Language", "")).upper()
                if lang == "PT" or m not in achadas:
                    achadas[m] = str(r.get("ProductDescription", "")).strip()
        except requests.HTTPError as e:
            log(f"   descrições do lote {i // lote + 1} não lidas: {e}")
        for m in grupo:
            descricoes[m] = achadas.get(m, "")
        if (i // lote) % 20 == 0:
            salvar_cache()
    return descricoes


def ler_textos_pedido(pedidos, textos, lote=15):
    """{'pedido|item': texto do item no pedido} (A_PurchaseOrderItem.PurchaseOrderItemText)."""
    pend = sorted(p for p in pedidos if p and p not in textos.get("_lidos", []))
    textos.setdefault("_lidos", [])
    log(f"   Texto dos itens do pedido: {len(pend)} pedido(s) a consultar")
    for i in range(0, len(pend), lote):
        grupo = pend[i:i + lote]
        filtro = " or ".join(f"PurchaseOrder eq '{p}'" for p in grupo)
        try:
            for r in paginar(f"{BASE_PO}/A_PurchaseOrderItem", {"$filter": filtro,
                                                                "$select": "PurchaseOrder,PurchaseOrderItem,PurchaseOrderItemText"}, 1000):
                textos[f"{str(r.get('PurchaseOrder', '')).strip()}|{str(r.get('PurchaseOrderItem', '')).lstrip('0')}"] = \
                    str(r.get("PurchaseOrderItemText", "")).strip()
        except requests.HTTPError as e:
            log(f"   pedidos do lote {i // lote + 1} não lidos: {e}")
        textos["_lidos"].extend(grupo)
        if (i // lote) % 20 == 0:
            log(f"   {min(i + lote, len(pend))}/{len(pend)} pedido(s)")
            salvar_cache()
    return textos


def ler_classificacao_pedido(pedidos, classif, lote=15):
    """{'pedido|item': {cat, uso, cc, ativo, conta}} -- classificação contábil do item do pedido
    (A_PurchaseOrderItem.AccountAssignmentCategory/BR_MaterialUsage + A_PurOrdAccountAssignment)."""
    classif.setdefault("_lidos", [])
    pend = sorted(p for p in pedidos if p and p not in classif["_lidos"])
    log(f"   Classificação contábil dos pedidos: {len(pend)} pedido(s) a consultar")
    for i in range(0, len(pend), lote):
        grupo = pend[i:i + lote]
        filtro = " or ".join(f"PurchaseOrder eq '{p}'" for p in grupo)
        try:
            for r in paginar(f"{BASE_PO}/A_PurchaseOrderItem", {"$filter": filtro, "$select":
                             "PurchaseOrder,PurchaseOrderItem,AccountAssignmentCategory,BR_MaterialUsage"}, 1000):
                k = f"{str(r.get('PurchaseOrder', '')).strip()}|{str(r.get('PurchaseOrderItem', '')).lstrip('0')}"
                classif.setdefault(k, {}).update({"cat": str(r.get("AccountAssignmentCategory") or "").strip(),
                                                  "uso": str(r.get("BR_MaterialUsage") or "").strip()})
            for r in paginar(f"{BASE_PO}/A_PurOrdAccountAssignment", {"$filter": filtro, "$select":
                             "PurchaseOrder,PurchaseOrderItem,CostCenter,MasterFixedAsset,GLAccount"}, 1000):
                k = f"{str(r.get('PurchaseOrder', '')).strip()}|{str(r.get('PurchaseOrderItem', '')).lstrip('0')}"
                d = classif.setdefault(k, {})
                for campo, origem in (("cc", "CostCenter"), ("ativo", "MasterFixedAsset"), ("conta", "GLAccount")):
                    if not d.get(campo):
                        d[campo] = str(r.get(origem) or "").strip()
        except requests.HTTPError as e:
            log(f"   classificação do lote {i // lote + 1} não lida: {e}")
        classif["_lidos"].extend(grupo)
        if (i // lote) % 20 == 0:
            log(f"   {min(i + lote, len(pend))}/{len(pend)} pedido(s)")
            salvar_cache()
    return classif


def pedido_do_item(fatura, item_nf):
    """Pedido/item do item da NF: no SAP o item da NF da fatura é o item da fatura x 10 (10, 20, ...).
    Se não bater, devolve vazio (as colunas 'Pedidos da fatura' continuam mostrando todos)."""
    try:
        n = int(str(item_nf)) // 10
    except ValueError:
        return "", ""
    for inv_item, po, po_item in fatura.get("pos", []):
        if inv_item == str(n):
            return po, po_item
    return "", ""


# ─────────────────────────────────────────────────────────────────────────────
#  Cache
# ─────────────────────────────────────────────────────────────────────────────
_cache = {}


def carregar_cache(usar):
    global _cache
    if usar and os.path.exists(CACHE):
        _cache = json.load(open(CACHE, encoding="utf-8"))
        log(f"Cache carregado ({CACHE}).")
    _cache.setdefault("faturas", {})
    _cache.setdefault("itens", {"_lidas": [], "dados": {}})
    _cache.setdefault("produtos", {})
    _cache.setdefault("descricoes", {})
    _cache.setdefault("textos_pedido", {})
    _cache.setdefault("classif_pedido", {})


def salvar_cache():
    tmp = CACHE + ".tmp"
    json.dump(_cache, open(tmp, "w", encoding="utf-8"))
    os.replace(tmp, CACHE)


# ─────────────────────────────────────────────────────────────────────────────
#  Excel
# ─────────────────────────────────────────────────────────────────────────────
CAB = PatternFill("solid", fgColor="1F4E78")
VERDE = PatternFill("solid", fgColor="E2EFDA")
AMARELO = PatternFill("solid", fgColor="FFF2CC")
VERMELHO = PatternFill("solid", fgColor="FCE4D6")


def aba(wb, titulo, cabecalho, linhas, larguras=None, cor_col=None):
    ws = wb.create_sheet(titulo)
    ws.append(cabecalho)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = CAB
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for l in linhas:
        ws.append(l)
    if cor_col is not None:
        cores = {"Regra candidata": VERDE, "Quase regra": AMARELO, "Dividido": VERMELHO}
        for row in ws.iter_rows(min_row=2):
            f = cores.get(row[cor_col].value)
            if f:
                for c in row:
                    c.fill = f
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, w in enumerate(larguras or [], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    return ws


def classificar(contas: Counter):
    n = sum(contas.values())
    conta, q = contas.most_common(1)[0]
    pct = q / n
    if len(contas) == 1 and n >= MIN_REGRA:
        c = "Regra candidata"
    elif pct >= PCT_QUASE and n >= MIN_QUASE:
        c = "Quase regra"
    elif len(contas) == 1:
        c = "Poucos casos"
    else:
        c = "Dividido"
    return n, conta, pct, c


def agrupar(linhas, chave_fn, nome_conta):
    grupos = defaultdict(Counter)
    valores = defaultdict(float)
    extras = defaultdict(set)
    for l in linhas:
        k = chave_fn(l)
        grupos[k][l["conta"]] += 1
        valores[k] += l["valor"]
        extras[k].add(l["ncm"])
    saida = []
    for k, contas in grupos.items():
        n, conta, pct, cls = classificar(contas)
        outras = "; ".join(f"{c} {nome_conta(c)} ({q})" for c, q in contas.most_common()[1:])
        saida.append((k, n, round(valores[k], 2), len(contas), conta, nome_conta(conta), round(pct * 100, 1),
                      cls, outras, len(extras[k])))
    ordem = {"Dividido": 0, "Quase regra": 1, "Regra candidata": 2, "Poucos casos": 3}
    saida.sort(key=lambda x: (ordem.get(x[7], 9), -x[1]))
    return saida


def gerar_excel(linhas, faturas, nome_conta, saida, anos):
    wb = Workbook()
    wb.remove(wb.active)
    todos = [l for l in linhas if l["conta"]]
    # CT-e / frete (CFOP x.35x: aquisição de serviço de transporte) tem conta própria por natureza do frete;
    # vai para uma aba separada para não misturar com a conta das mercadorias.
    fretes = [l for l in todos if str(l["cfop"])[1:3] == "35"]
    ok = [l for l in todos if str(l["cfop"])[1:3] != "35"]

    # Resumo
    contas_uso = defaultdict(Counter)
    for l in ok:
        contas_uso[l["uso"]][l["conta"]] += 1
    ws = wb.create_sheet("Resumo")
    info = [
        ["Relatório", "Conta contábil x material x utilização (compras com pedido)"],
        ["Gerado em", datetime.now().strftime("%d/%m/%Y %H:%M")],
        ["Anos", ", ".join(anos)],
        ["Faturas lançadas com NF", len(faturas)],
        ["Faturas consideradas", len({l['fatura'] for l in ok})],
        ["Itens de mercadoria/serviço (abas de análise)", len(ok)],
        ["Itens de frete / CT-e (aba Fretes CT-e)", len(fretes)],
        ["Itens sem conta (ignorados)", len(linhas) - len(todos)],
        [],
        ["Classificação", "Significado"],
        ["Regra candidata", f"sempre a mesma conta (>= {MIN_REGRA} ocorrências)"],
        ["Quase regra", f"conta dominante >= {int(PCT_QUASE * 100)}% (>= {MIN_QUASE} ocorrências): ver exceções"],
        ["Dividido", "mais de uma conta sem predominância: precisa de definição do contábil"],
        ["Poucos casos", f"uma conta só, mas menos de {MIN_REGRA} ocorrências"],
        [],
        ["Observações"],
        ["• Conta = conta do Razão do item da NF (conta de destino: estoque, consumo, ativo), não a conta transitória da fatura."],
        ["• Valor = base aproximada do item (CBS/IBS em 2026; PIS/COFINS antes). Para regra, olhar a QUANTIDADE."],
        ["• NCM vem da NF/pedido (é o que vale na prática; o cadastro do material pode ser genérico)."],
        ["• Excluídas: faturas estornadas/de estorno, notas de crédito e faturas não lançadas (status != 5)."],
        ["• Fretes (CFOP x.35x, CT-e) ficam só na aba 'Fretes CT-e' -- a conta deles segue a natureza do frete, não o material."],
        ["• Abas top-down: 'Conta (top-down)' resume cada conta; 'Conta → Material (top-down)' desce até utilização, material,"
         " pedidos e texto do item no pedido."],
        ["• Pedido/item = item da fatura (item da NF ÷ 10). Quando não bate, fica em branco; 'Pedidos da fatura' traz todos."],
    ]
    for r in info:
        ws.append(r)
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 90
    for c in ("A1", "A9"):
        ws[c].font = Font(bold=True)

    cab = ["Qtd itens", "Valor aprox. (R$)", "Nº contas", "Conta dominante", "Nome", "% dominante",
           "Classificação", "Outras contas (qtd)", "Nº NCMs"]

    # Utilização x Conta (visão geral)
    g = agrupar(ok, lambda l: (USOS.get(l["uso"], l["uso"]),), nome_conta)
    aba(wb, "Uso", ["Utilização"] + cab, [list(k) + list(r) for k, *r in g],
        [38, 10, 14, 10, 14, 28, 11, 16, 60, 9], cor_col=7)

    # Utilização + NCM x Conta (base da regra)
    g = agrupar(ok, lambda l: (USOS.get(l["uso"], l["uso"]), l["ncm"]), nome_conta)
    aba(wb, "Uso + NCM", ["Utilização", "NCM"] + cab, [list(k) + list(r) for k, *r in g],
        [38, 13, 10, 14, 10, 14, 28, 11, 16, 60, 9], cor_col=8)

    # Tipo de material + Utilização
    g = agrupar(ok, lambda l: (l["tipo_mat"], USOS.get(l["uso"], l["uso"])), nome_conta)
    aba(wb, "Tipo mat + Uso", ["Tipo de material", "Utilização"] + cab, [list(k) + list(r) for k, *r in g],
        [24, 38, 10, 14, 10, 14, 28, 11, 16, 60, 9], cor_col=8)

    # Material + Utilização (genérico = vários NCMs)
    g = agrupar(ok, lambda l: (l["material"], l["desc_material"], l["tipo_mat"], l["grupo_mat"],
                               USOS.get(l["uso"], l["uso"])), nome_conta)
    linhas_m = []
    for k, *r in g:
        generico = "SIM" if r[-1] > 1 else ""
        linhas_m.append(list(k) + list(r) + [generico])
    aba(wb, "Material + Uso", ["Material", "Descrição material", "Tipo mat.", "Grupo merc.", "Utilização"] + cab +
        ["Genérico (vários NCMs)?"], linhas_m, [14, 34, 20, 12, 38, 10, 14, 10, 14, 28, 11, 16, 60, 9, 12], cor_col=11)

    # ── Top-down: da CONTA para utilização, material e pedido ──────────────────
    total_conta = Counter(l["conta"] for l in ok)
    det = defaultdict(lambda: {"n": 0, "valor": 0.0, "ncm": Counter(), "forn": set(), "cc": Counter(),
                               "pedidos": Counter(), "textos": Counter()})
    for l in ok:
        d = det[(l["conta"], USOS.get(l["uso"], l["uso"]), l["material"], l["desc_material"])]
        d["n"] += 1
        d["valor"] += l["valor"]
        d["ncm"][l["ncm"]] += 1
        d["forn"].add(l["fornecedor"])
        if l["cc"]:
            d["cc"][l["cc"]] += 1
        if l["pedido"]:
            d["pedidos"][l["pedido"]] += 1
        if l["texto_pedido"]:
            d["textos"][l["texto_pedido"]] += 1
    top = lambda c, n=3: "; ".join(f"{k} ({q})" for k, q in c.most_common(n))
    linhas_td = []
    for (conta, uso, mat, desc), d in det.items():
        linhas_td.append([conta, nome_conta(conta), total_conta[conta], uso, mat, desc, d["n"],
                          round(d["n"] / total_conta[conta] * 100, 1), round(d["valor"], 2), top(d["ncm"], 5),
                          len(d["forn"]), top(d["cc"]), top(d["pedidos"]), top(d["textos"])])
    linhas_td.sort(key=lambda x: (-x[2], x[0], -x[6]))
    aba(wb, "Conta → Material (top-down)",
        ["Conta", "Nome conta", "Itens na conta", "Utilização", "Material", "Descrição material", "Qtd itens",
         "% da conta", "Valor aprox. (R$)", "NCMs (qtd)", "Nº fornecedores", "Centros de custo (qtd)",
         "Pedidos (qtd)", "Texto do item no pedido (qtd)"],
        linhas_td, [11, 28, 10, 30, 13, 34, 9, 8, 13, 30, 9, 24, 30, 50])

    resumo_conta = defaultdict(lambda: {"uso": Counter(), "mat": Counter(), "ncm": set(), "valor": 0.0, "cc": Counter()})
    for l in ok:
        r = resumo_conta[l["conta"]]
        r["uso"][USOS.get(l["uso"], l["uso"])] += 1
        r["mat"][f"{l['material'] or '(sem material)'} {l['desc_material']}".strip()] += 1
        r["ncm"].add(l["ncm"])
        r["valor"] += l["valor"]
        if l["cc"]:
            r["cc"][l["cc"]] += 1
    linhas_rc = []
    for conta, r in sorted(resumo_conta.items(), key=lambda x: -total_conta[x[0]]):
        uso_dom, q_dom = r["uso"].most_common(1)[0]
        linhas_rc.append([conta, nome_conta(conta), total_conta[conta], round(r["valor"], 2), len(r["uso"]),
                          uso_dom, round(q_dom / total_conta[conta] * 100, 1), len(r["mat"]), len(r["ncm"]),
                          top(r["uso"], 5), top(r["mat"], 5), top(r["cc"], 5)])
    aba(wb, "Conta (top-down)",
        ["Conta", "Nome conta", "Qtd itens", "Valor aprox. (R$)", "Nº utilizações", "Utilização dominante",
         "% dominante", "Nº materiais", "Nº NCMs", "Utilizações (qtd)", "Principais materiais (qtd)",
         "Centros de custo (qtd)"],
        linhas_rc, [11, 28, 9, 13, 9, 30, 9, 9, 8, 50, 70, 30])

    # Classificação do pedido (A imobilizado / K centro de custo / ...) x conta
    g = agrupar([l for l in ok if l.get("classif_pedido") is not None],
                lambda l: (l.get("classif_pedido") or "(sem pedido/classif.)", "Projeto" if l.get("projeto") else "",
                           USOS.get(l["uso"], l["uso"])), nome_conta)
    aba(wb, "Classificação + Uso", ["Classificação (pedido)", "Projeto (A+PRJ)", "Utilização"] + cab,
        [list(k) + list(r) for k, *r in g], [12, 10, 38, 10, 14, 10, 14, 28, 11, 16, 60, 9], cor_col=9)

    # Projetos (A + PRJ): utilização por mês -- quanto a trava 'projeto só com utilização 3' barraria
    proj = defaultdict(Counter)
    for l in ok:
        if l.get("projeto"):
            proj[str(l["data"])[:7]][l["uso"]] += 1
    linhas_p = [[m, sum(c.values()), c.get("3", 0), sum(q for u, q in c.items() if u != "3"),
                 "; ".join(f"{USOS.get(u, u)} ({q})" for u, q in c.most_common())] for m, c in sorted(proj.items())]
    aba(wb, "Projetos por mês", ["Mês", "Itens de projeto (A + PRJ)", "Com utilização 3", "Outra utilização",
                                 "Utilizações (qtd)"], linhas_p, [9, 12, 12, 12, 90])

    # Fretes / CT-e
    g = agrupar(fretes, lambda l: (USOS.get(l["uso"], l["uso"]), str(l["cfop"])[:4]), nome_conta)
    aba(wb, "Fretes CT-e", ["Utilização", "CFOP"] + cab, [list(k) + list(r) for k, *r in g],
        [38, 8, 10, 14, 10, 14, 28, 11, 16, 60, 9], cor_col=8)

    # Conta x Utilização (inverso: o que cai em cada conta)
    por_conta = defaultdict(Counter)
    for l in ok:
        por_conta[l["conta"]][USOS.get(l["uso"], l["uso"])] += 1
    linhas_c = [[c, nome_conta(c), sum(u.values()), "; ".join(f"{k} ({q})" for k, q in u.most_common())]
                for c, u in sorted(por_conta.items(), key=lambda x: -sum(x[1].values()))]
    aba(wb, "Conta x Uso", ["Conta", "Nome", "Qtd itens", "Utilizações (qtd)"], linhas_c, [12, 30, 10, 100])

    # Base detalhada
    cols = ["ano", "data", "fatura", "fornecedor", "nf", "item", "cfop", "uso", "ncm", "material", "desc_material",
            "tipo_mat", "grupo_mat", "centro", "cc", "conta", "nome_conta", "cod_imposto", "valor", "pedido",
            "item_pedido", "texto_pedido", "pedidos_fatura", "classif_pedido", "uso_pedido", "cc_pedido",
            "ativo_pedido", "conta_pedido", "projeto"]
    aba(wb, "Base", ["Ano", "Data lçto", "Fatura", "Fornecedor", "NF interna", "Item NF", "CFOP", "Utilização", "NCM",
                     "Material", "Descrição material", "Tipo mat.", "Grupo merc.", "Centro", "Centro de custo", "Conta",
                     "Nome conta", "Cód. imposto", "Valor aprox.", "Pedido", "Item pedido", "Texto do item no pedido",
                     "Pedidos da fatura", "Classificação (pedido)", "Utilização (pedido hoje)", "Centro de custo (pedido)",
                     "Imobilizado (pedido)", "Conta no pedido", "Projeto (A + PRJ)?"],
        [[l.get(c, "") for c in cols] for l in linhas],
        [6, 11, 12, 11, 12, 8, 9, 10, 12, 13, 34, 9, 11, 7, 12, 11, 28, 8, 12, 12, 8, 40, 24, 9, 9, 14, 12, 11, 9])

    wb.save(saida)


def carregar_nomes_contas(caminho):
    nomes = {}
    if caminho and os.path.exists(caminho):
        wb = load_workbook(caminho, read_only=True, data_only=True)
        ws = wb["cr"] if "cr" in wb.sheetnames else None
        if ws is not None:
            for r in ws.iter_rows(min_row=2, values_only=True):
                if r and r[0]:
                    nomes[str(r[0]).strip()] = str(r[1] or "").strip()
        log(f"Nomes de {len(nomes)} conta(s) carregados de {caminho}.")
    return nomes


# ─────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--anos", nargs="+", default=["2025", "2026"])
    ap.add_argument("--empresa", default="BR01")
    ap.add_argument("--limite", type=int, help="máx. de faturas por ano (teste rápido)")
    ap.add_argument("--plano-contas", default="lançamentos_2026.xlsx", help="xlsx com aba 'cr' (conta, nome)")
    ap.add_argument("--saida", default=f"relatorio_conta_contabil_{datetime.now():%Y%m%d}.xlsx")
    ap.add_argument("--sem-cache", action="store_true")
    a = ap.parse_args()

    carregar_cache(not a.sem_cache)
    nomes = carregar_nomes_contas(a.plano_contas)

    chave_fat = f"{','.join(a.anos)}|{a.empresa}|{a.limite}|v2"  # v2: com os pedidos de cada item
    if chave_fat not in _cache["faturas"]:
        log(f"1/3 Faturas {', '.join(a.anos)} ({a.empresa})...")
        _cache["faturas"][chave_fat] = listar_faturas(a.anos, a.empresa, a.limite)
        salvar_cache()
    faturas = _cache["faturas"][chave_fat]

    validas = [f for f in faturas if f["nf"] and not f["estornada"] and not f["credito"]]
    lancadas = [f for f in validas if f["status"] == "5"]
    if not lancadas:
        log("   Nenhuma fatura com status 5 -- considerando todas as não estornadas.")
        lancadas = validas
    sem_nf = sum(1 for f in faturas if not f["nf"])
    log(f"   {len(faturas)} lida(s); {len(lancadas)} lançada(s) com NF; {sem_nf} sem NF vinculada")

    por_nf = {}
    for f in lancadas:
        por_nf.setdefault(f["nf"], f)
    ler_itens_nf(sorted(por_nf), _cache["itens"])
    salvar_cache()

    itens = [it for k, it in _cache["itens"]["dados"].items() if it["nf"] in por_nf]
    entradas = [it for it in itens if str(it["cfop"])[:1] in ("1", "2", "3")]
    ler_tipos_material(sorted({it["material"] for it in entradas}), _cache["produtos"])
    salvar_cache()
    ler_descricoes(sorted({it["material"] for it in entradas}), _cache["descricoes"])
    salvar_cache()
    ler_textos_pedido({po for f in lancadas for _, po, _ in f.get("pos", [])}, _cache["textos_pedido"])
    salvar_cache()
    ler_classificacao_pedido({po for f in lancadas for _, po, _ in f.get("pos", [])}, _cache["classif_pedido"])
    salvar_cache()

    linhas = []
    for it in entradas:
        f = por_nf[it["nf"]]
        p = _cache["produtos"].get(it["material"], {})
        tipo = p.get("tipo", "")
        po, po_item = pedido_do_item(f, it["item"])
        linhas.append({
            "ano": f["ano"], "data": f["data"], "fatura": f["fatura"], "fornecedor": f["fornecedor"],
            "nf": it["nf"], "item": it["item"], "cfop": it["cfop"], "uso": it["uso"], "ncm": it["ncm"],
            "material": it["material"], "tipo_mat": f"{tipo} - {TIPOS[tipo]}" if tipo in TIPOS else (tipo or "(sem material)"),
            "grupo_mat": p.get("grupo", ""), "centro": it["centro"], "cc": it["cc"], "conta": it["conta"],
            "nome_conta": nomes.get(it["conta"], ""), "cod_imposto": it["cod_imposto"],
            "valor": round(valor_item(it["bases"]), 2),
            "desc_material": _cache["descricoes"].get(it["material"], ""),
            "pedido": po, "item_pedido": po_item,
            "texto_pedido": _cache["textos_pedido"].get(f"{po}|{po_item}", "") if po else "",
            "pedidos_fatura": ", ".join(sorted({x[1] for x in f.get("pos", [])})),
        })
        c = _cache["classif_pedido"].get(f"{po}|{po_item}", {}) if po else {}
        cc_ped = c.get("cc", "")
        linhas[-1].update({
            "classif_pedido": c.get("cat", ""), "uso_pedido": c.get("uso", ""), "cc_pedido": cc_ped,
            "ativo_pedido": c.get("ativo", ""), "conta_pedido": c.get("conta", ""),
            "projeto": "SIM" if c.get("cat", "").upper() == "A" and "PRJ" in cc_ped.upper() else "",
        })
        if not linhas[-1]["cc"] and cc_ped:
            linhas[-1]["cc"] = cc_ped  # a CDS da NF não traz centro de custo; usa o do pedido
    log(f"Gerando Excel com {len(linhas)} item(ns) de NF de entrada...")
    gerar_excel(linhas, lancadas, lambda c: nomes.get(c, ""), a.saida, a.anos)
    log(f"Pronto: {a.saida}")


if __name__ == "__main__":
    main()
