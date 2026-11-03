"""Ajuste das parcelas na ENTRADA do card na fase Pagamento (depois da validação contábil).

Pedido do João (14/10/2026): ajustar as parcelas logo depois do lançamento obrigava a desfazer o ajuste sempre que
o lançamento precisava ser estornado na fase Contábil (NF 555333, NF 44902: o reparcelamento sobrava em aberto).
Agora o lançamento só AVISA a divergência; o ajuste roda quando o card entra em Pagamento:
  - para cada XML do card, acha a fatura lançada (válida: não estornada e não é estorno) pelo nº da NF;
  - roda a auditoria das parcelas, o ajuste (Fase 3) e a compensação dos adiantamentos do card;
  - tudo certo (ajustado, ou já estava igual à NF) -> comenta no card e move para Concluído;
  - qualquer problema -> comenta, avisa a Contas a Pagar no Slack e o card FICA em Pagamento (ajuste manual);
  - houve ajuste (de valor, vencimento ou adiantamento) -> grava no campo Auditoria Fiscal (com a linha
    "✅ Contas a Pagar: ... ajustado automaticamente" no topo, usada nos relatórios do Pipefy) e avisa a
    Contas a Pagar no Slack, como era quando o ajuste rodava no lançamento.
Card sem XML ou sem fatura lançada para os XMLs (lançamento manual fora da automação): não faz nada, fica em Pagamento.
O bloqueio de pagamento (A) continua com a Contas a Pagar.

Configuração (.env): PAGAMENTO_PHASE_ID (gatilho; vazio = desligado, ajuste logo após o lançamento como antes) e
CONCLUIDO_PHASE_ID (destino). Sem CONCLUIDO_PHASE_ID, ajusta mas não move.

Teste pelo servidor (sem --enviar só lê: mostra a fatura achada, a auditoria e o que seria feito):
  sudo -u www-data venv/bin/python fase_pagamento.py CARD_ID [CARD_ID ...] [--enviar]
"""
import argparse
import logging
import os
import re
import sys
import threading

import pipefy_client as pipefy
import sap_client as sap
import xml_parser
from config import MAPA_FILIAIS_CNPJ

log = logging.getLogger("fase_pagamento")

CONCLUIDO_PHASE_ID = os.getenv("CONCLUIDO_PHASE_ID", "").strip()
PAGAMENTO_PHASE_ID = os.getenv("PAGAMENTO_PHASE_ID", "").strip()
CAMPO_CARD_FINALIZADO = os.getenv("CAMPO_CARD_FINALIZADO", "card_finalizado")  # "Card finalizado?" (radio)
VALOR_CARD_FINALIZADO = os.getenv("VALOR_CARD_FINALIZADO", "SIM")  # mesmo valor que a analista do Contas a Pagar marca


def referencia_nf(dados_nf: dict) -> str:
    """Mesmo formato do lançamento (SupplierInvoiceIDByInvcgParty): '<nº>-<série sem zeros à esquerda>';
    NFS-e só '<nº>'."""
    if dados_nf.get("tipo") == "NFSE":
        return str(dados_nf.get("numero_nf", ""))
    serie = str(dados_nf.get("serie", "0")).strip().lstrip("0") or "0"
    return f"{dados_nf.get('numero_nf', '')}-{serie}"


def faturas_validas(ref: str, empresa: str) -> list:
    """Faturas da empresa com essa referência que não estão estornadas e não são estorno."""
    import ajuste_parcelas as ap
    leitor = ap.LeitorSAP()
    ref_q = ref.replace("'", "''")
    fs = leitor._get(f"{sap.BASE}/A_SupplierInvoice?$filter=SupplierInvoiceIDByInvcgParty eq '{ref_q}' and "
                     f"CompanyCode eq '{empresa}'&$select=SupplierInvoice,FiscalYear,InvoicingParty,IsReversed,"
                     f"IsReversal,SupplierInvoiceOrigin&$format=json").get("results", [])
    return [f for f in fs if not f.get("IsReversed") and not f.get("IsReversal")]


def _nfs_da_memoria(memoria: str) -> set:
    return {m.group(1).strip() for m in re.finditer(r"NF\s*([^:|]+?)\s*:\s*\d{10}/\d{4}", memoria or "")}


def _faturas_da_memoria(memoria: str, nf: str) -> set:
    """Faturas (nº SAP) que a automação registrou no card para essa NF ('NF 4428: 5128234232/2026')."""
    return {m.group(2) for m in re.finditer(r"NF\s*([^:|]+?)\s*:\s*(\d{10})/\d{4}", memoria or "")
            if m.group(1).strip() == str(nf).strip()}


def _fase_atual(card_id: str) -> str:
    """ID da fase em que o card está agora ('' se não conseguir ler)."""
    try:
        r = pipefy.requests.post(pipefy.PIPEFY_API_URL, headers=pipefy.HEADERS, timeout=15, json={
            "query": "query($id: ID!) { card(id: $id) { current_phase { id } } }", "variables": {"id": str(card_id)}})
        return str((((r.json().get("data") or {}).get("card") or {}).get("current_phase") or {}).get("id") or "")
    except Exception as e:
        log.info("[Pagamento] card %s: não li a fase atual: %s", card_id, e)
        return ""


_EM_ANDAMENTO, _TRAVA = set(), threading.Lock()


def processar_pagamento(card_id: str, enviar: bool = True, alertar=None, out=None) -> dict:
    """Evita rodar duas vezes o mesmo card: o Pipefy às vezes manda o webhook de entrada em Pagamento repetido
    (card 1434444198, 16/10: a segunda execução achou o card já em Concluído e comentou que não conseguiu mover)."""
    with _TRAVA:
        if card_id in _EM_ANDAMENTO:
            log.info("[Pagamento] card %s: já está sendo processado; webhook repetido ignorado", card_id)
            return {"status": "ignorado", "texto": "em andamento"}
        _EM_ANDAMENTO.add(card_id)
    try:
        return _processar_pagamento(card_id, enviar=enviar, alertar=alertar, out=out)
    finally:
        with _TRAVA:
            _EM_ANDAMENTO.discard(card_id)


def _processar_pagamento(card_id: str, enviar: bool = True, alertar=None, out=None) -> dict:
    """Devolve {"status": "concluido"|"pendente"|"ignorado", "texto": ...}."""
    out = out or (lambda m: log.info("[Pagamento] %s", m))
    out(f"card {card_id}: entrada em Pagamento")
    if enviar and PAGAMENTO_PHASE_ID:
        fase = _fase_atual(card_id)
        if fase and fase != PAGAMENTO_PHASE_ID:
            out(f"o card já não está em Pagamento (fase {fase}): nada a fazer")
            return {"status": "ignorado", "texto": "fora de Pagamento"}
    card_data = pipefy.buscar_card(card_id)
    card_data["card_id"] = card_id
    xml_urls = card_data.get("xml_urls") or []
    if not xml_urls:
        out("sem XML no card: nada a fazer (fica em Pagamento)")
        return {"status": "ignorado", "texto": "sem XML"}
    nfs_automacao = _nfs_da_memoria(card_data.get("documentos_existentes", ""))

    blocos, pendencias, achou_alguma, vistos, ajustou = [], [], False, set(), False
    nao_xml, lidos = [], 0
    for url in xml_urls:
        try:
            conteudo = pipefy.baixar_xml(url)
        except ValueError as e:
            # Anexo com cara de XML que é PDF/imagem (card 1441815143, NF 54058): não é a NF, ignora.
            nao_xml.append(str(e))
            out(f"anexo ignorado (não é XML): {e}")
            continue
        except Exception as e:
            pendencias.append(f"XML não baixado: {e}")
            continue
        try:
            dados_nf = xml_parser.parsear(conteudo)
        except Exception as e:
            pendencias.append(f"XML não lido: {e}")
            continue
        lidos += 1
        nf = str(dados_nf.get("numero_nf", "") or "")
        if nf in vistos:
            continue
        vistos.add(nf)
        filial = MAPA_FILIAIS_CNPJ.get(str(dados_nf.get("cnpj_destinatario", "")).strip())
        if not filial:
            pendencias.append(f"NF {nf}: CNPJ do destinatário não é de uma filial cadastrada")
            continue
        ref = referencia_nf(dados_nf)
        try:
            fs = faturas_validas(ref, filial["company_code"])
        except Exception as e:
            pendencias.append(f"NF {nf}: erro ao procurar a fatura no SAP ({e})")
            continue
        if not fs:
            if nf in nfs_automacao:
                pendencias.append(f"NF {nf}: não achei fatura válida (não estornada) com a referência {ref}")
            else:
                out(f"NF {nf}: sem fatura lançada com a referência {ref} (lançamento fora da automação?)")
            continue
        if len(fs) > 1:
            # Mesma referência <NF>-<série> de outro fornecedor (card 1402326224, NF 4428: a 5115130238 é de
            # outro fornecedor, de fevereiro). Fica com a fatura que a automação registrou neste card.
            do_card = _faturas_da_memoria(card_data.get("documentos_existentes", ""), nf)
            if do_card:
                fs = [f for f in fs if str(f["SupplierInvoice"]) in do_card] or fs
        if len(fs) > 1:
            pendencias.append(f"NF {nf}: {len(fs)} faturas válidas com a referência {ref} "
                              f"({', '.join(f['SupplierInvoice'] for f in fs)}); ajuste manual")
            continue
        achou_alguma = True
        doc, ano = str(fs[0]["SupplierInvoice"]), str(fs[0]["FiscalYear"])
        out(f"NF {nf}: fatura {doc}/{ano}")
        if not enviar:
            blocos.append(_simular(doc, ano, filial["company_code"], dados_nf, card_data, nf, out))
            continue
        try:
            r = sap.ajustar_parcelas_da_fatura(doc, ano, filial["company_code"], dados_nf, card_data)
        except Exception as e:
            log.exception("[Pagamento] NF %s: erro no ajuste automático", nf)
            r = {"ok": False, "ajustou": False, "texto": f"⚠️ Erro no ajuste automático: {e}"}
        out(f"NF {nf}: {'ok' if r['ok'] else 'PENDENTE'}{' (ajustou)' if r.get('ajustou') else ''}"
            + ("" if r["ok"] else f" -- {str(r.get('texto') or '').strip().splitlines()[-1][:200] if r.get('texto') else ''}"))
        ajustou = ajustou or bool(r.get("ajustou"))
        blocos.append(f"[NF {nf}]\nFatura {doc}/{ano}\n" + (r["texto"] or "✅ Parcelas já iguais à NF: nada a ajustar."))
        if not r["ok"]:
            pendencias.append(f"NF {nf}: ajuste não concluído")

    if nao_xml and not lidos and not pendencias:
        pendencias.append("nenhum anexo do card é um XML válido")
    if not achou_alguma and not pendencias:
        out("nenhuma fatura da automação para os XMLs do card: nada a fazer (fica em Pagamento)")
        return {"status": "ignorado", "texto": ""}

    status = "concluido" if not pendencias else "pendente"
    texto = "🤖 Ajuste de parcelas (entrada em Pagamento)\n\n" + "\n\n".join(blocos)
    if not enviar:
        if pendencias:
            texto += ("\n\n⚠️ Fica em Pagamento para a Contas a Pagar:\n" + "\n".join(f"• {p}" for p in pendencias))
        elif CONCLUIDO_PHASE_ID:
            texto += "\n\n➡️ Parcelas conferidas: o card seria movido para Concluído."
        out("SIMULAÇÃO -- nada foi alterado no SAP nem no card. Resultado previsto: "
            + ("mover para Concluído" if status == "concluido" else "ficar em Pagamento"))
        return {"status": status, "texto": texto}

    # Move ANTES de comentar, para o comentário dizer o que de fato aconteceu.
    motivo_mov = ""
    if status == "concluido" and CONCLUIDO_PHASE_ID:
        motivo_mov = concluir_card(card_id)
        if motivo_mov:
            status = "pendente"
            out(f"NÃO consegui mover para Concluído: {motivo_mov}")
        else:
            out("movido para Concluído")
    if pendencias:
        texto += ("\n\n⚠️ Fica em Pagamento para a Contas a Pagar:\n" + "\n".join(f"• {p}" for p in pendencias))
        out("fica em Pagamento: " + "; ".join(pendencias))
    elif motivo_mov:
        texto += (f"\n\n⚠️ Parcelas conferidas, mas o Pipefy não deixou mover o card para Concluído: {motivo_mov}. "
                  "Mover manualmente.")
    elif CONCLUIDO_PHASE_ID:
        texto += ("\n\n➡️ Parcelas conferidas: card movido para Concluído. O bloqueio de pagamento continua com a "
                  "Contas a Pagar.")
    try:
        pipefy.comentar_card(card_id, texto)
    except Exception as e:
        out(f"falha ao comentar no card: {e}")
    if ajustou:
        try:
            gravar_na_auditoria(card_id, "\n\n".join(blocos))
        except Exception as e:
            out(f"falha ao gravar no campo Auditoria Fiscal: {e}")
    if alertar:
        try:
            if status == "concluido" and ajustou:
                alertar(f"✅ *Parcelas ajustadas automaticamente (entrada em Pagamento)*\n{texto}\n\n"
                        f"🔗 https://app.pipefy.com/open-cards/{card_id}", "Contas a Pagar", "warning")
            elif status == "pendente":
                alertar(f"⚠️ *Card ficou em Pagamento*\n{texto}\n\n"
                        f"🔗 https://app.pipefy.com/open-cards/{card_id}", "Contas a Pagar", "warning")
        except Exception as e:
            out(f"falha no alerta do Slack: {e}")
    return {"status": status, "texto": texto}


def concluir_card(card_id: str) -> str:
    """Como a Contas a Pagar faz: marca "Card finalizado?" (card_finalizado) = SIM e move para Concluído.
    Devolve '' se deu certo, senão o motivo (o Pipefy recusa a mudança de fase se faltar campo obrigatório)."""
    # ID fixo do campo (fase Pagamento): a consulta dos campos do card só devolve os JÁ preenchidos, então
    # procurar pelo rótulo falhava justamente quando o campo estava vazio (card 1403404270, 15/10).
    try:
        pipefy.atualizar_campo(card_id, CAMPO_CARD_FINALIZADO, VALOR_CARD_FINALIZADO)
    except Exception as e:
        log.warning("[Pagamento] card %s: não consegui marcar 'Card finalizado?': %s", card_id, e)
    try:
        motivo = pipefy.mover_card_verificado(card_id, CONCLUIDO_PHASE_ID)
    except Exception as e:
        return f"erro: {e}"
    if motivo and "already in the destination phase" in motivo:
        return ""  # alguém (ou outra execução) já moveu para Concluído
    return motivo


def gravar_na_auditoria(card_id: str, texto_ajuste: str):
    """Acrescenta o resultado do ajuste ao campo Auditoria Fiscal (o texto do lançamento continua lá) e refaz a
    linha-resumo "✅ Contas a Pagar: ..." no topo (pipefy_client.com_linha_de_ajuste)."""
    atual = pipefy.ler_campo(card_id, pipefy.ID_CAMPO_AUDITORIA) or ""
    linhas = atual.splitlines()
    if linhas and linhas[0].lstrip().startswith(pipefy.PREFIXO_AJUSTE_CP):
        atual = "\n".join(linhas[1:])
    novo = (atual.rstrip() + "\n\n" if atual.strip() else "") + "[Ajuste de parcelas na entrada em Pagamento]\n" + texto_ajuste
    pipefy.atualizar_campo(card_id, pipefy.ID_CAMPO_AUDITORIA, pipefy.com_linha_de_ajuste(novo))


def _simular(doc, ano, empresa, dados_nf, card_data, nf, out) -> str:
    """Só leitura: auditoria + plano da Fase 3 + adiantamentos do card que seriam compensados."""
    import requests
    import ajuste_parcelas as ap
    alerta = sap._auditar_fase2_financeiro(requests.Session(), doc, ano, empresa, dados_nf)
    out(f"NF {nf}: auditoria das parcelas: {'DIVERGENTE' if alerta else 'iguais à NF'}")
    linhas = [f"[NF {nf}] (simulação)", f"Fatura {doc}/{ano}"]
    leitor = ap.LeitorSAP()
    lanc = leitor.documento_contabil(doc, ano, empresa)
    fatura = leitor.fatura(doc, ano)
    if alerta:
        linhas.append(alerta)
        plano = ap.montar_plano(fatura, leitor.linhas(lanc, ano, empresa), dados_nf, card_data.get("card_id"))
        acao = {"alterar_data": "ajustaria só o vencimento", "ajustar": "reparcelaria (RE novo + compensação)"}
        linhas.append(f"→ Fase 3: {acao.get(plano['acao'], 'não ajustaria: ' + str(plano.get('motivo')))}")
    adtos = leitor.adiantamentos_abertos(fatura.get("InvoicingParty"), empresa)
    pares = ap.casar_adiantamentos_do_card(leitor.linhas(lanc, ano, empresa), adtos, card_data.get("card_id"))
    for p, a, n in pares:
        linhas.append(f"→ compensaria a parcela {n} com o adiantamento {a['AccountingDocument']}/{a.get('FiscalYear')} "
                      f"\"{a.get('DocumentItemText') or ''}\"")
    for l in linhas:
        out(l)
    return "\n".join(linhas)


def main():
    ap_ = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap_.add_argument("cards", nargs="+")
    ap_.add_argument("--enviar", action="store_true", help="executa de verdade (ajusta, comenta e move o card)")
    a = ap_.parse_args()
    for c in a.cards:
        print("=" * 78)
        r = processar_pagamento(c, enviar=a.enviar, out=print)
        print(f"\nResultado: {r['status']}")
        if r["texto"]:
            print("\n--- comentário" + (" (previsto)" if not a.enviar else "") + " ---\n" + r["texto"])
        if a.enviar and r["status"] == "pendente":
            print("(alerta do Slack só é enviado pelo serviço)")


if __name__ == "__main__":
    sys.exit(main())
