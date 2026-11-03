"""Relatório (só leitura): faturas de fornecedor ESTORNADAS cujas parcelas ficaram EM ABERTO junto com o estorno.

Caso que motivou: NF 44383 (card 1463331410). A fatura 5142765916 (lançamento 5136241053) foi estornada pela
fatura 5130322136 (lançamento 5135229269), mas as parcelas dos dois lançamentos ficaram em aberto (somam zero).
A compensação automática mensal que limpava esses pares parou de rodar.

O par vem do vínculo do PRÓPRIO SAP entre a fatura e o estorno (A_SupplierInvoice: IsReversed + ReverseDocument),
não de valor ou texto. Para cada par, compara os itens do fornecedor (sem razão especial) dos dois lançamentos:
  - "compensar"        -> todos os itens dos dois lançamentos em aberto, mesmo fornecedor e soma zero.
                          Seguro para compensar: o relatório mostra o comando (--compensar-itens DOC/ANO DOC/ANO).
  - "revisar"          -> parte em aberto e parte já compensada (ex.: a fatura foi reparcelada antes do estorno),
                          ou a soma em aberto não fecha. Não compensar sem olhar (Contas a Pagar).
  - "ok"               -> nada em aberto (o estorno compensou, ou alguém já limpou). Só entra na contagem.
  - "sem lançamento"   -> não achei o lançamento contábil de uma das faturas (YY1_FATURA_DEPARA).
Não cobre estorno de lançamento contábil avulso (KZ/SA estornado no app de lançamentos), só de fatura.

Uso (servidor):
  sudo -u www-data venv/bin/python relatorio_estornos_abertos.py [--anos 2026] [--empresa BR01 ...] [--saida testes_ajuste]
"""
import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal

from ajuste_parcelas import TOLERANCIA_IGUAL, LeitorSAP, _data_odata, _dec
from investigar_irf import EMPRESAS, _gravar, _paginar


def faturas_estornadas(leitor, ano, empresa):
    sel = ("SupplierInvoice,FiscalYear,CompanyCode,InvoicingParty,SupplierInvoiceIDByInvcgParty,PostingDate,"
           "CreationDate,SupplierInvoiceOrigin,IsReversed,IsReversal,ReverseDocument,ReverseDocumentFiscalYear,"
           "InvoiceGrossAmount")
    todas = _paginar(leitor, f"{leitor.sc.BASE}/A_SupplierInvoice?$filter=FiscalYear eq '{ano}' and CompanyCode eq "
                             f"'{empresa}'&$select={sel}&$orderby=SupplierInvoice", 2000)
    return [f for f in todas if f.get("IsReversed") and str(f.get("ReverseDocument") or "").strip()]


def mapa_depara(leitor, ano, empresa):
    """{fatura+ano: lançamento contábil} da empresa no ano."""
    mapa = {}
    for r in _paginar(leitor, f"{leitor.sc.BASE_DEPARA}/YY1_FATURA_DEPARA?$filter=CompanyCode eq '{empresa}' and "
                              f"FiscalYear eq '{ano}'", 5000):
        ref = str(r.get("OriginalReferenceDocument") or "")
        if len(ref) >= 14 and ref[:10].isdigit() and r.get("AccountingDocument"):
            mapa.setdefault(ref[:14], str(r["AccountingDocument"]))
    return mapa


def _card(linhas):
    """ID do card no texto do item ('NF 44383, ID 1463331410, ...')."""
    for l in linhas:
        m = re.search(r"\bID\s*(\d{9,10})(?!\d)", str(l.get("DocumentItemText") or ""), re.I)
        if m:
            return m.group(1)
    return ""


def levantar(leitor, anos, empresas, out=print):
    """Pares fatura estornada x estorno com a situação das parcelas. Devolve (linhas, resumo):
    linhas = pares que não estão 'ok' (dicionários do CSV); resumo = Counter[(empresa, status)].
    Usado pelo relatório (main) e pela compensação em lote (compensar_estornos_abertos.py)."""
    linhas_csv, resumo = [], Counter()

    for empresa in empresas:
        out(f"\n===== {empresa} =====")
        try:
            itens_por_doc = defaultdict(list)
            depara = {}
            estornadas = []
            for ano in anos:
                for it in _paginar(leitor, f"{leitor.sc.BASE_SUPPLIER_ITEMS}/YY1_SUPPLIER_LINE_ITEMS?$filter="
                                           f"CompanyCode eq '{empresa}' and FiscalYear eq '{ano}'", 5000):
                    itens_por_doc[(str(it.get("AccountingDocument")), str(it.get("FiscalYear") or ano))].append(it)
                depara.update(mapa_depara(leitor, ano, empresa))
                estornadas += faturas_estornadas(leitor, ano, empresa)
        except Exception as e:
            out(f"[aviso] {empresa}: {e}")
            continue
        out(f"Faturas estornadas: {len(estornadas)}")

        def lancamento(fatura, ano):
            doc = depara.get(f"{fatura}{ano}")
            if not doc:
                doc = leitor.documento_contabil(fatura, ano, empresa)
            return str(doc) if doc else None

        def itens(doc, ano):
            chave = (doc, str(ano))
            if chave not in itens_por_doc:
                itens_por_doc[chave] = leitor.linhas(doc, ano, empresa)
            return [l for l in itens_por_doc[chave]
                    if str(l.get("Supplier") or "").strip() and not str(l.get("SpecialGLCode") or "").strip()]

        for f in estornadas:
            fat, ano = str(f["SupplierInvoice"]), str(f["FiscalYear"])
            est, ano_est = str(f["ReverseDocument"]), str(f.get("ReverseDocumentFiscalYear") or ano)
            doc_o, doc_e = lancamento(fat, ano), lancamento(est, ano_est)
            base = {"empresa": empresa, "fornecedor": str(f.get("InvoicingParty") or "").strip(),
                    "nf": f.get("SupplierInvoiceIDByInvcgParty") or "", "fatura": f"{fat}/{ano}",
                    "origem": "automação" if f.get("SupplierInvoiceOrigin") == "G" else "app (manual)",
                    "lancada_em": str(_data_odata(f.get("PostingDate")) or ""), "valor_fatura": f.get("InvoiceGrossAmount"),
                    "estorno": f"{est}/{ano_est}", "lanc_fatura": f"{doc_o}/{ano}" if doc_o else "",
                    "lanc_estorno": f"{doc_e}/{ano_est}" if doc_e else ""}
            if not doc_o or not doc_e:
                status, extra = "sem lançamento", {}
            else:
                io, ie = itens(doc_o, ano), itens(doc_e, ano_est)
                todos = io + ie
                abertos = [l for l in todos if not str(l.get("ClearingJournalEntry") or "").strip()]
                soma_ab = sum((_dec(l["AmountInCompanyCodeCurrency"]) for l in abertos), Decimal("0"))
                forns = {str(l.get("Supplier") or "").strip() for l in todos}
                if not abertos:
                    status = "ok"
                elif (len(abertos) == len(todos) and io and ie and len(forns) == 1
                      and abs(soma_ab) <= TOLERANCIA_IGUAL):
                    status = "compensar"
                else:
                    status = "revisar"
                extra = {"card": _card(todos), "itens": len(todos), "itens_abertos": len(abertos),
                         "soma_aberta": str(soma_ab),
                         "valor_aberto_fatura": str(sum((_dec(l["AmountInCompanyCodeCurrency"]) for l in io
                                                         if not str(l.get("ClearingJournalEntry") or "").strip()),
                                                        Decimal("0"))),
                         "comando": (f"sudo -u www-data venv/bin/python ajuste_parcelas.py --empresa {empresa} "
                                     f"--fatura {fat} --ano {ano} --compensar-itens {doc_o}/{ano} {doc_e}/{ano_est}")
                         if status == "compensar" else ""}
            resumo[(empresa, status)] += 1
            if status != "ok":
                linhas_csv.append({**base, "status": status, **extra})
    return linhas_csv, resumo


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--anos", nargs="+", default=[str(date.today().year)])
    ap.add_argument("--empresa", nargs="+", default=list(EMPRESAS))
    ap.add_argument("--saida", default="testes_ajuste")
    a = ap.parse_args()
    os.makedirs(a.saida, exist_ok=True)
    leitor = LeitorSAP()
    linhas_csv, resumo = levantar(leitor, a.anos, a.empresa, out=lambda m: print(m, flush=True))

    print("\n===== RESUMO (faturas estornadas por situação das parcelas) =====")
    for (emp, st), n in sorted(resumo.items()):
        print(f"  {emp}  {st:15} {n}")
    for st in ("compensar", "revisar", "sem lançamento"):
        ls = [l for l in linhas_csv if l["status"] == st]
        if not ls:
            continue
        print(f"\n--- {st} ({len(ls)}) ---")
        for l in ls:
            print(f"  {l['empresa']} forn {l['fornecedor']:8} NF {l['nf']:14} fatura {l['fatura']} ({l['origem']}) "
                  f"x estorno {l['estorno']} | lanç. {l['lanc_fatura']} x {l['lanc_estorno']} | "
                  f"abertos {l.get('itens_abertos', '-')}/{l.get('itens', '-')} soma {l.get('soma_aberta', '-')} "
                  f"card {l.get('card') or '-'}")
    print("\nCSV:")
    _gravar(os.path.join(a.saida, "estornos_abertos.csv"), linhas_csv)


if __name__ == "__main__":
    sys.exit(main())
