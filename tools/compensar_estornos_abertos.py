"""Compensação em lote: fatura ESTORNADA x ESTORNO com as parcelas dos dois em aberto (somam zero).

Problema A do doc "Estornos com parcelas em aberto": o estorno de uma fatura cria as mesmas parcelas com sinal
contrário e, em alguns casos, o SAP não compensa as duas. A TI não vai tratar por dentro do SAP (29/10/2026), então
este script faz o que antes era feito à mão, par a par, com `ajuste_parcelas.py --compensar-itens`.

Só entra o que o relatorio_estornos_abertos.py classifica como "compensar":
  - o par vem do vínculo do próprio SAP (A_SupplierInvoice IsReversed/ReverseDocument), não de valor ou texto;
  - TODOS os itens do fornecedor (sem razão especial) dos dois lançamentos estão em aberto;
  - um único fornecedor, e a soma é zero (± R$ 0,005).
"revisar" e "sem lançamento" nunca são tocados. Antes de enviar cada par, os itens são relidos no SAP e as
condições conferidas de novo (se algo mudou desde o levantamento, o par fica para a próxima rodada).

Cada par vira uma compensação (KZ) só com os itens dele (JournalEntryBulkClearingRequest, o mesmo de
--compensar-itens), confirmada pela releitura dos itens.

Uso (servidor, na pasta do projeto):
  venv/bin/python compensar_estornos_abertos.py                 # simulação: lista o que seria compensado
  venv/bin/python compensar_estornos_abertos.py --enviar        # compensa no SAP
  venv/bin/python compensar_estornos_abertos.py --enviar --limite 5     # só os 5 primeiros pares
Agendamento (cron, seg/qua/sex): ver o doc do projeto. Resultado em <saida>/compensar_estornos_AAAAMMDD_HHMM.csv.
"""
import argparse
import os
import sys
import time
import uuid
from datetime import date, datetime
from decimal import Decimal

import requests
from requests.auth import HTTPBasicAuth

import ajuste_parcelas as ap
from investigar_irf import EMPRESAS, _gravar
from relatorio_estornos_abertos import levantar

TEXTO_CABECALHO = "Comp estorno fatura"   # DocumentHeaderText (até 25 caracteres)


def _itens_fornecedor(leitor, ref, empresa):
    """Itens do fornecedor (sem razão especial) do lançamento 'DOC/ANO', lidos agora do SAP."""
    doc, ano = ref.split("/")
    return [l for l in leitor.linhas(doc, ano, empresa)
            if str(l.get("Supplier") or "").strip() and not str(l.get("SpecialGLCode") or "").strip()]


def _aberto(l):
    return not str(l.get("ClearingJournalEntry") or "").strip()


def conferir_par(leitor, par):
    """Relê os dois lançamentos e confere as condições de 'compensar'. Devolve (linhas, problema ou '')."""
    io = _itens_fornecedor(leitor, par["lanc_fatura"], par["empresa"])
    ie = _itens_fornecedor(leitor, par["lanc_estorno"], par["empresa"])
    todos = io + ie
    if not io or not ie:
        return [], "um dos lançamentos não tem item do fornecedor"
    if not all(_aberto(l) for l in todos):
        return [], "algum item já foi compensado desde o levantamento"
    forns = {str(l.get("Supplier") or "").strip() for l in todos}
    if forns != {par["fornecedor"]}:
        return [], f"fornecedor diferente do esperado ({sorted(forns)})"
    soma = sum((ap._dec(l["AmountInCompanyCodeCurrency"]) for l in todos), Decimal("0"))
    if abs(soma) > ap.TOLERANCIA_IGUAL:
        return [], f"itens não somam zero (soma {soma})"
    return sorted(todos, key=lambda l: (str(l["AccountingDocument"]), str(l["AccountingDocumentItem"]))), ""


def compensar(leitor, sessao, par, linhas, enviar, out):
    """Monta, valida e (com enviar) manda a compensação dos itens do par. Devolve (situação, documento, message_id)."""
    empresa, fornecedor = par["empresa"], par["fornecedor"]
    message_id = f"AJEST_{uuid.uuid4().hex[:10].upper()}"
    itens = [(l["AccountingDocument"], str(l["FiscalYear"]), str(l["AccountingDocumentItem"])) for l in linhas]
    env = ap.montar_clearing_itens(empresa, fornecedor, itens, message_id, TEXTO_CABECALHO)
    erros = ap.validar(env, ap.WSDL_CLEAR, "Compensação")
    if erros:
        return "erro: " + "; ".join(erros), "", message_id
    if not enviar:
        return "simulado", "", message_id
    ep = ap._endpoint(ap.WSDL_CLEAR, leitor.sc.SAP_BASE_URL.rstrip("/") + "/sap/bc/srt/scs_ext/sap/journalentrybulkclearingreques")
    ap.enviar_wsrm(sessao, env, ep, ap.ACT_CLEAR, "Compensação", out)
    doc_comp = ""
    for _ in range(ap.POLL_TENTATIVAS):
        time.sleep(ap.POLL_INTERVALO_S)
        pend = []
        for l in linhas:
            atual = next((x for x in leitor.linhas(l["AccountingDocument"], str(l["FiscalYear"]), empresa)
                          if str(x.get("AccountingDocumentItem")) == str(l["AccountingDocumentItem"])), {})
            if _aberto(atual):
                pend.append(l)
            else:
                doc_comp = str(atual.get("ClearingJournalEntry"))
        if not pend:
            return "compensado", doc_comp, message_id
    return "não confirmado (ver Message Dashboard)", "", message_id


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ano = date.today().year
    p.add_argument("--anos", nargs="+", default=[str(ano - 1), str(ano)])
    p.add_argument("--empresa", nargs="+", default=list(EMPRESAS))
    p.add_argument("--enviar", action="store_true", help="compensa no SAP (sem isso, só simula)")
    p.add_argument("--limite", type=int, default=0, help="no máximo N pares nesta rodada (0 = todos)")
    p.add_argument("--saida", default="logs_estornos")
    a = p.parse_args()
    os.makedirs(a.saida, exist_ok=True)

    def out(m):
        print(m, flush=True)

    inicio = datetime.now()
    out(f"=== Compensação de estornos em aberto | {inicio:%d/%m/%Y %H:%M} | "
        f"{'ENVIO' if a.enviar else 'SIMULAÇÃO'} | anos {', '.join(a.anos)} | empresas {', '.join(a.empresa)}")
    leitor = ap.LeitorSAP()
    linhas, resumo = levantar(leitor, a.anos, a.empresa, out=out)
    pares = [l for l in linhas if l["status"] == "compensar"]
    revisar = sum(1 for l in linhas if l["status"] == "revisar")
    out(f"\nPares para compensar: {len(pares)} | para revisar (não tocados): {revisar}")
    if a.limite:
        pares = pares[:a.limite]

    from config import SAP_USERNAME, SAP_PASSWORD
    sessao = requests.Session()
    sessao.auth = HTTPBasicAuth(SAP_USERNAME, SAP_PASSWORD)
    resultado = []
    for i, par in enumerate(pares, 1):
        rotulo = (f"[{i}/{len(pares)}] {par['empresa']} forn {par['fornecedor']} NF {par['nf']} fatura {par['fatura']} "
                  f"x estorno {par['estorno']} ({par['lanc_fatura']} x {par['lanc_estorno']})")
        out(f"\n{rotulo}")
        situacao, doc_comp, msg = "", "", ""
        try:
            itens, problema = conferir_par(leitor, par)
            if problema:
                situacao = f"pulado: {problema}"
            else:
                out(f"   {len(itens)} itens, soma zero")
                situacao, doc_comp, msg = compensar(leitor, sessao, par, itens, a.enviar, out)
        except Exception as e:
            situacao = f"erro: {e}"
        out(f"   -> {situacao}{(' pelo documento ' + doc_comp) if doc_comp else ''}")
        resultado.append({**{k: par.get(k, "") for k in ("empresa", "fornecedor", "nf", "fatura", "estorno",
                                                          "lanc_fatura", "lanc_estorno", "origem", "card", "itens")},
                          "situacao": situacao, "compensacao": doc_comp, "message_id": msg})

    cont = {}
    for r in resultado:
        chave = r["situacao"].split(":")[0]
        cont[chave] = cont.get(chave, 0) + 1
    out(f"\n=== Resumo: {len(resultado)} pares | " + " | ".join(f"{k}: {v}" for k, v in sorted(cont.items()))
        + f" | duração {(datetime.now() - inicio).seconds // 60} min")
    _gravar(os.path.join(a.saida, f"compensar_estornos_{inicio:%Y%m%d_%H%M}.csv"), resultado)
    falhas = sum(1 for r in resultado if r["situacao"].startswith(("erro", "não confirmado")))
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
