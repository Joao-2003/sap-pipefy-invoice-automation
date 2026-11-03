"""Validação automática da NF na fase "📄Validação da Nota Fiscal" do pipe da Logística (antes do recebimento).

Pedido do João (15/10/2026): os erros que hoje só aparecem no lançamento (fase "Notas Fiscais com Pedido", pipe do
Financeiro) passam a ser apontados já na Logística, para o card chegar ao Financeiro pronto para lançar.
Roda as MESMAS checagens do lançamento (sap_client.lancar_fatura_completo com somente_validar=True), sem lançar nada:
  - CNPJ do destinatário é de uma filial nossa; NF já lançada no SAP (mesma referência, não estornada);
  - centro do pedido x filial da NF (inclusive ativo); utilização de pedido de projeto; utilização em branco;
  - condição de pagamento do pedido x duplicatas do XML (mesma regra/tolerância do Financeiro);
  - CNPJ do emissor x fornecedor do pedido; casamento dos itens (NCM, preço, saldo do pedido).
O recebimento (MIGO) é feito DEPOIS desta fase (Recebimento Sistêmico), então não é exigido aqui.
Não cobre o que o SAP só responde no lançamento (diferença de imposto, conta de imposto, período fechado).

Resultado: comentário no card; com erro, "Nota validada com sucesso?" = Não. Nunca marca Sim (fica para o humano).
Sem aviso no Slack (a tratativa é da responsável pela fase).
Card sem XML, sem pedido ou NF de serviço: não faz nada.

Configuração (.env): VALIDACAO_LOGISTICA_PHASE_ID=342179599 liga o gatilho (webhook card.move para essa fase).

Teste pelo servidor (sem --enviar só mostra o resultado; não comenta nem mexe no card):
  sudo -u www-data venv/bin/python validacao_logistica.py CARD_ID [CARD_ID ...] [--enviar]
"""
import argparse
import logging
import os
import sys

import pipefy_client as pipefy
import sap_client as sap
import xml_parser
from config import MAPA_FILIAIS_CNPJ

log = logging.getLogger("validacao_logistica")

CAMPO_PEDIDO = os.getenv("LOGISTICA_CAMPO_PEDIDO", "n_pedido_sap")
CAMPO_NF_SERVICO = "uma_nf_de_servi_o"
CAMPO_VALIDADA = "nota_validada_com_sucesso"


def _responsavel(erro: str, card_data: dict) -> str:
    """Mesma classificação do lançamento (main.responsavel_do_card). No serviço o main já está carregado."""
    try:
        m = sys.modules.get("main")
        if m is None or not hasattr(m, "responsavel_do_card"):
            m = sys.modules.get("__main__")
        if m is None or not hasattr(m, "responsavel_do_card"):
            import main as m
        return m.responsavel_do_card(erro, card_data)
    except Exception:
        return ""


def _preparar_sem_recebimento(card_data: dict):
    """Nesta fase ainda não há recebimento: o casamento usa o saldo do pedido, sem exigir MIGO."""
    card_data["validacao_sem_recebimento"] = True
    for it in card_data.get("itens_po", []):
        if it.get("gr_based_iv"):
            it["qtd_recebida_total"] = max(float(it.get("qtd_recebida_total", 0) or 0), float(it.get("qtd_pedida", 0) or 0))


def validar_card(card_id: str, enviar: bool = True, out=None) -> dict:
    """Devolve {"status": "ok"|"erro"|"ignorado", "texto": ...}."""
    out = out or (lambda m: log.info("[Validação Logística] %s", m))
    out(f"card {card_id}: entrada em Validação da Nota Fiscal")
    card_data = pipefy.buscar_card(card_id)
    card_data["card_id"] = card_id
    try:
        pedido = pipefy._limpar_pedido(pipefy.ler_campo(card_id, CAMPO_PEDIDO) or "")
        servico = str(pipefy.ler_campo(card_id, CAMPO_NF_SERVICO) or "").strip().lower() == "sim"
    except Exception as e:
        out(f"não consegui ler os campos do card: {e}")
        return {"status": "ignorado", "texto": str(e)}
    if pedido:
        card_data["numero_pedido_sap"] = pedido
    xml_urls = card_data.get("xml_urls") or []
    if servico:
        out("NF de serviço: validação automática não se aplica")
        return {"status": "ignorado", "texto": "NF de serviço"}
    if not xml_urls:
        out("sem XML no card: nada a validar")
        return {"status": "ignorado", "texto": "sem XML"}
    if not card_data.get("numero_pedido_sap"):
        out("sem Nº Pedido SAP no card: nada a validar")
        return {"status": "ignorado", "texto": "sem pedido"}

    blocos, erros, vistos = [], [], set()
    for url in xml_urls:
        nf = "?"
        try:
            dados_nf = xml_parser.parsear(pipefy.baixar_xml(url))
            nf = str(dados_nf.get("numero_nf", "") or "?")
            if nf in vistos:
                continue
            vistos.add(nf)
            filial = MAPA_FILIAIS_CNPJ.get(str(dados_nf.get("cnpj_destinatario", "")).strip())
            if not filial:
                raise ValueError("Nota Fiscal Inválida: Nenhum dos CNPJs envolvidos no XML pertence às nossas filiais.")
            _checar_ja_lancada(dados_nf, filial["company_code"])
            dados_card = dict(card_data)
            sap_po = sap.buscar_empresa_por_po(dados_card["numero_pedido_sap"])
            if sap_po:
                dados_card["itens_po"] = sap_po.get("itens_po", [])
                dados_card["cnpj_fornecedor_sap"] = sap_po.get("cnpj_fornecedor_sap", "")
                dados_card["payment_terms"] = sap_po.get("payment_terms", "")
                dados_card["condicoes_encontradas"] = sap_po.get("condicoes_encontradas", [])
            _preparar_sem_recebimento(dados_card)
            r = sap.lancar_fatura_completo(dados_nf, dados_card, filial, somente_validar=True)
            cond = r.get("payment_terms") or ""
            blocos.append(f"✅ NF {nf}: pronta para lançar (pedido {dados_card['numero_pedido_sap']}"
                          + (f", condição {cond}" if cond else "") + ").")
            out(f"NF {nf}: ok")
        except Exception as e:
            msg = str(e).replace("MATCH_FAIL: ", "")
            resp = _responsavel(msg, card_data)
            erros.append(nf)
            blocos.append(f"❌ NF {nf}: {msg}" + (f"\n   Responsável sugerido: {resp}" if resp else ""))
            out(f"NF {nf}: ERRO -- {msg[:200]}")

    status = "erro" if erros else "ok"
    texto = ("🤖 Validação automática da NF (antes do recebimento)\n\n" + "\n\n".join(blocos) + "\n\n"
             + ("⚠️ Corrija os pontos acima antes de enviar ao Financeiro. Depois de corrigir, a validação roda de novo "
                "se o card voltar para esta fase." if erros else
                "Conferido o mesmo que o lançamento confere (exceto o que o SAP só responde no lançamento). "
                "A decisão de marcar \"Nota validada com sucesso?\" continua com a responsável pela fase."))
    if not enviar:
        out("SIMULAÇÃO -- nada foi alterado no card. Resultado: " + status)
        return {"status": status, "texto": texto}
    try:
        pipefy.comentar_card(card_id, texto)
    except Exception as e:
        out(f"falha ao comentar no card: {e}")
    if erros:
        try:
            pipefy.atualizar_campo(card_id, CAMPO_VALIDADA, "Não")
        except Exception as e:
            out(f"falha ao marcar 'Nota validada com sucesso?' = Não: {e}")
    return {"status": status, "texto": texto}


def _checar_ja_lancada(dados_nf: dict, empresa: str):
    """NF já lançada no SAP (fatura válida com a mesma referência) -> erro, como no Financeiro."""
    try:
        from fase_pagamento import faturas_validas, referencia_nf
        fs = faturas_validas(referencia_nf(dados_nf), empresa)
    except Exception as e:
        log.info("[Validação Logística] não consegui checar duplicidade: %s", e)
        return
    if fs:
        raise ValueError(f"NF {referencia_nf(dados_nf)} já lançada no SAP: fatura "
                         f"{', '.join(f['SupplierInvoice'] + '/' + str(f['FiscalYear']) for f in fs)}. "
                         "Conferir se o card está duplicado.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cards", nargs="+")
    ap.add_argument("--enviar", action="store_true", help="comenta no card e marca 'Nota validada' = Não se houver erro")
    a = ap.parse_args()
    for c in a.cards:
        print("=" * 78)
        r = validar_card(c, enviar=a.enviar, out=print)
        print(f"\nResultado: {r['status']}")
        if r["texto"]:
            print("\n--- comentário" + (" (previsto)" if not a.enviar else "") + " ---\n" + r["texto"])


if __name__ == "__main__":
    sys.exit(main())
