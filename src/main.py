import hashlib
import hmac
import json
import logging
import os
import sys
import traceback
import base64
import threading
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from flask_basicauth import BasicAuth
from slack_sdk.webhook import WebhookClient

import pipefy_client  as pipefy
import sap_client     as sap
import xml_parser
import pop_rules
from config import (
    WEBHOOK_SECRET,
    TRIGGER_PHASE_ID,
    MAPA_FILIAIS_CNPJ,
)

PHASE_ID_CARTAO = os.getenv("PHASE_ID_CARTAO", "340270423")

# Pular a fase Contábil quando a auditoria POP confere tudo (combinado com o Contábil em 28/10/2026):
# - só utilizações em CONTABIL_AUTO_USOS (0, 1 e 2 por enquanto; o 4 entra quando o motor fiscal passar a gerar
#   1101/2101 no lugar de 1506/2506, e o 3 continua sempre no Contábil);
# - nenhum item com CFOP de imobilizado (1551/2551), mesmo com outra utilização;
# - todas as notas do lote auditadas nesta rodada com status "ok" (divergência ou "não verificado" = Contábil).
# O card vai direto para a fase Pagamento (PAGAMENTO_PHASE_ID). Desligar com PULAR_CONTABIL=0.
PULAR_CONTABIL = os.getenv("PULAR_CONTABIL", "1").strip() == "1"
CONTABIL_AUTO_USOS = {u.strip() for u in os.getenv("CONTABIL_AUTO_USOS", "0,1,2").split(",") if u.strip()}
CONTABIL_CFOPS_SEMPRE = [c.strip() for c in os.getenv("CONTABIL_CFOPS_SEMPRE", "1551,2551").split(",") if c.strip()]
# Campos do card (fase Notas Fiscais com Pedido) preenchidos no lançamento, para relatório do Contábil.
CAMPO_DIFAL = os.getenv("PIPEFY_CAMPO_DIFAL", "difal")
CAMPO_CFOP = os.getenv("PIPEFY_CAMPO_CFOP", "cfop")
CAMPO_UTILIZACAO = os.getenv("PIPEFY_CAMPO_UTILIZACAO", "utiliza_o")
CAMPO_CARD_VALIDADO = os.getenv("PIPEFY_CAMPO_CARD_VALIDADO", "card_validado")   # "Card validado?" = SIM ao pular


def decidir_pular_contabil(auditorias: list, nf_sem_auditoria: bool) -> tuple:
    """(pular, motivo) a partir das auditorias detalhadas das NFs do lote."""
    if not PULAR_CONTABIL:
        return False, "regra desligada (PULAR_CONTABIL=0)"
    if not sap.PAGAMENTO_PHASE_ID:
        return False, "fase Pagamento não configurada (PAGAMENTO_PHASE_ID)"
    # NF lançada numa rodada anterior deste card (lote que tinha falhado em outra NF): ela não é auditada de novo
    # agora, então conta como "não verificada".
    if nf_sem_auditoria or not auditorias:
        return False, "há nota do card não verificada nesta rodada (lançada numa rodada anterior)"
    for nf, aud in auditorias:
        if aud.get("status") != "ok":
            return False, f"NF {nf}: auditoria {'com divergência' if aud.get('status') == 'divergente' else 'não verificada'}"
        if not aud.get("itens"):
            return False, f"NF {nf}: nenhum item auditado"
        for it in aud["itens"]:
            if it.get("utilizacao") not in CONTABIL_AUTO_USOS:
                return False, f"NF {nf}: utilização {it.get('utilizacao') or 'vazia'} vai sempre para o Contábil"
            if any(str(it.get("cfop", "")).startswith(c) for c in CONTABIL_CFOPS_SEMPRE):
                return False, f"NF {nf}: CFOP {it.get('cfop')} (imobilizado) vai sempre para o Contábil"
    return True, "auditoria OK em todas as notas"


def campos_fiscais(auditorias: list) -> dict:
    """Valores dos campos DIFAL, CFOP e Utilização do card a partir das auditorias do lote."""
    itens = [it for _, aud in auditorias for it in aud.get("itens", [])]
    cfops = list(dict.fromkeys(str(it.get("cfop")) for it in itens if it.get("cfop")))
    usos = list(dict.fromkeys(str(it.get("utilizacao")) for it in itens if it.get("utilizacao")))
    nomes = {"0": "Revenda", "1": "Industrialização", "2": "Consumo", "3": "Imobilizado", "4": "Consumo ativ. principal"}
    campos = {}
    if cfops:
        campos[CAMPO_CFOP] = ", ".join(cfops)
    if usos:
        campos[CAMPO_UTILIZACAO] = ", ".join(f"{u} - {nomes.get(u, '')}".rstrip(" -") for u in usos)
    # DIFAL só é preenchido quando existe valor; sem DIFAL (ou sem documento fiscal), o campo fica vazio.
    difal = round(sum(float(it.get("difal") or 0) for it in itens), 2)
    if cfops and difal > 0.005:
        campos[CAMPO_DIFAL] = "R$ " + f"{difal:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return campos

# ─────────────────────────────────────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout), 
        logging.FileHandler("erros_lancamento.txt", encoding="utf-8")
    ],
)
log = logging.getLogger("main")

app = Flask(__name__)
app.config['BASIC_AUTH_USERNAME'] = os.getenv('BASIC_AUTH_USERNAME')
app.config['BASIC_AUTH_PASSWORD'] = os.getenv('BASIC_AUTH_PASSWORD')
basic_auth = BasicAuth(app)

# ─────────────────────────────────────────────────────────────────────────────
#  Integração com Slack e Lógica de Erros
# ─────────────────────────────────────────────────────────────────────────────

def enviar_alerta_slack(mensagem: str, responsavel: str = "Financeiro", nivel: str = "warning"):
    mapa_canais = {
        "Financeiro": [os.getenv("SLACK_WEBHOOK_FINANCEIRO")],
        "Compras": [os.getenv("SLACK_WEBHOOK_COMPRAS")],
        "Excelência Operacional": [os.getenv("SLACK_WEBHOOK_EXCELENCIA")],
        "Logística": [os.getenv("SLACK_WEBHOOK_LOGISTICA")],
        "Logística e Financeiro": [os.getenv("SLACK_WEBHOOK_LOGISTICA"), os.getenv("SLACK_WEBHOOK_FINANCEIRO")],
        "TI / Cadastro": [os.getenv("SLACK_WEBHOOK_FINANCEIRO")], 
        "Contas a Pagar": [os.getenv("SLACK_WEBHOOK_CONTAS_PAGAR", os.getenv("SLACK_WEBHOOK_FINANCEIRO"))]
    }
    
    urls_destino = []
    canal_geral = os.getenv("SLACK_WEBHOOK_URL")
    if canal_geral:
        urls_destino.append(canal_geral)
        
    canais_especificos = [url for url in mapa_canais.get(responsavel, []) if url]
    urls_destino.extend(canais_especificos)
    urls_destino = list(set(urls_destino))

    if not urls_destino:
        return

    cor = "#FFA500" if nivel == "warning" else "#FF0000"
    
    for url in urls_destino:
        webhook = WebhookClient(url)
        try:
            response = webhook.send(
                text="⚠️ Alerta de Lançamento SAP", 
                attachments=[{
                    "fallback": mensagem, "color": cor,
                    "title": f"⚠️ Ação Necessária: {responsavel}",
                    "text": mensagem, "footer": "Automação Fiscal S/4HANA"
                }]
            )
            if response.status_code != 200:
                log.error("Erro do Slack SDK ao enviar para %s: %s", url, response.body)
        except Exception as e:
            log.error("Falha fatal ao enviar alerta para o Slack (%s): %s", url, e)

MAPA_ERROS_RESPONSAVEL = {
    "Divergência Comercial": "Compras",
    "O item": "Compras",
    "Faltam condições de pagamento": "Compras",
    "condições de pagamento diferentes": "Compras",
    "Incompatibilidade de Preço": "Compras",
    "Inconsistência no item": "Compras",
    "Não há itens compatíveis": "Compras",
    "Falta de Saldo": "Compras",   # quantidade dos pedidos menor que a da NF (NF 869952, 29/10/2026); antes caía no padrão Financeiro
    "NCM divergente": "Compras",
    "Código de Imposto": "Compras",
    "Tax Code": "Compras",
    "RevFatEM": "Compras", 
    "Bloqueio de Filial": "Compras",
    "nao esta liberado": "Compras",   
    "Configuração de Recebimento Incorreta": "Compras",
    "entrada de mercadorias": "Logística e Financeiro",
    "ReferenceDocument": "Logística e Financeiro",
    "MIGO": "Logística e Financeiro",
    "Unidade incompatível": "Excelência Operacional",
    "Unidade de medida": "Excelência Operacional",
    "não tem regras para converter": "Excelência Operacional",
    "A finalidade": "Excelência Operacional",
    "O POP da empresa": "Excelência Operacional",
    "Não existe CFOP": "Excelência Operacional",
    "Company Code": "Excelência Operacional",
    "Local de Negócios": "Excelência Operacional",
    "O SAP recusou": "Excelência Operacional",
    "Nota Fiscal Inválida": "Compras",
    "CT-e Inválido": "Compras",
    "não especifica o Tomador": "Compras",
    "O número do Pedido de Compra": "Compras",
    "XML da NF não encontrado": "Financeiro",
    "XML inválido": "Financeiro",
    "Erro SAP": "Financeiro",
    "Saldo não é igual a zero": "Financeiro",
    "Limite de auto-correção": "Financeiro",
    "Falha sistêmica": "Financeiro",
    "Usuário já está processando": "Financeiro",
    "Bloqueio Antifraude": "Compras",
    "existe": "Financeiro",
    "Preencher o campo obrigatório": "Financeiro",
    "Períodos contábeis permitidos": "Financeiro"
}

def classificar_responsavel_erro(mensagem_erro: str) -> str:
    for trecho, responsavel in MAPA_ERROS_RESPONSAVEL.items():
        if trecho in mensagem_erro:
            return responsavel
    return "Financeiro"


def responsavel_do_card(mensagem_erro: str, card_data: dict = None) -> str:
    """Como classificar_responsavel_erro, mas card com etiqueta de FRETE (CT-e) é tratado pela
    Logística: o que iria para Compras vai para a Logística."""
    responsavel = classificar_responsavel_erro(mensagem_erro)
    operacoes = set((card_data or {}).get("operacoes") or [])
    if responsavel == "Compras" and "cte" in operacoes:
        return "Logística"
    return responsavel

# ─────────────────────────────────────────────────────────────────────────────
#  Orquestrador Principal LOTE (Pipefy Flow)
# ─────────────────────────────────────────────────────────────────────────────

def processar_card(card_id: str):
    log.info("═══ Iniciando processamento — card %s ═══", card_id)
    po_number = "Não identificado ainda"
    
    try:
        log.info("[1/7] Buscando dados do card Pipefy...")
        card_data = pipefy.buscar_card(card_id)
        
        xml_urls = card_data.get("xml_urls", [])
        po_number = card_data.get("numero_pedido_sap", "Sem Pedido no Card")
        doc_memoria = card_data.get("documentos_existentes", "")
        
        if not xml_urls:
            raise ValueError("XML da NF não encontrado nos anexos do card.")
        
        if not po_number or po_number == "Sem Pedido no Card":
            raise ValueError("O número do Pedido de Compra (PO) não foi preenchido no card do Pipefy. Impossível lançar a nota sem o pedido.")

        lista_sucessos = []
        lista_erros = []
        tipos_docs = []
        maior_score = 0
        relatorio_pop_batch = ""
        notas_vistas_neste_lote = set()
        auditorias = []            # (NF, auditoria detalhada) das notas lançadas nesta rodada
        nf_sem_auditoria = False   # alguma NF do card não foi auditada agora (já lançada antes)
        nfse_pendencias = []       # NFS-e lançadas nesta rodada: o que o Contas a Pagar completa à mão
        
        if doc_memoria:
            antigos = [s.strip() for s in doc_memoria.split("|") if s.strip()]
            lista_sucessos.extend(antigos)

        log.info(f"Encontrados {len(xml_urls)} arquivos para processar neste lote.")

        for idx, xml_url in enumerate(xml_urls, 1):
            numero_nf = "Desconhecida"
            try:
                log.info(f"[XML {idx}/{len(xml_urls)}] Baixando e parseando...")
                xml_bytes = pipefy.baixar_xml(xml_url)
                dados_nf = xml_parser.parsear(xml_bytes)
                numero_nf = dados_nf.get("numero_nf", "Desconhecida")
                tipos_docs.append(dados_nf.get("tipo", "OUTROS"))
                
                if numero_nf in notas_vistas_neste_lote:
                    log.info(f"⏩ NF {numero_nf} já foi processada neste lote. Evitando duplicata.")
                    continue
                notas_vistas_neste_lote.add(numero_nf)
                
                if f"NF {numero_nf}:" in doc_memoria:
                    nf_sem_auditoria = True
                    log.info(f"⏩ NF {numero_nf} já consta como lançada historicamente neste card. Pulando requisição SAP...")
                    continue

                cnpj_dest = str(dados_nf.get("cnpj_destinatario", "")).strip()
                config_filial = MAPA_FILIAIS_CNPJ.get(cnpj_dest)
                
                if not config_filial:
                    raise ValueError("Nota Fiscal Inválida: Nenhum dos CNPJs envolvidos no XML pertence às nossas filiais.")

                log.info(f"Buscando dados atualizados do Pedido {po_number} no SAP...")
                sap_po_data = sap.buscar_empresa_por_po(po_number)
                if sap_po_data:
                    card_data["itens_po"] = sap_po_data.get("itens_po", [])
                    card_data["cnpj_fornecedor_sap"] = sap_po_data.get("cnpj_fornecedor_sap", "")
                    card_data["payment_terms"] = sap_po_data.get("payment_terms", "")
                    card_data["condicoes_encontradas"] = sap_po_data.get("condicoes_encontradas", [])

                log.info(f"Lançando fatura da NF {numero_nf} no SAP S/4HANA...")
                resultado = sap.lancar_fatura_completo(
                    dados_nf       = dados_nf,
                    card_data      = card_data,
                    company_config = config_filial,
                )
                
                doc = resultado.get("supplier_invoice", "")
                ano = resultado.get("fiscal_year", "")
                score = resultado.get("risk_score", 0)
                alerta_financeiro = resultado.get("alerta_financeiro", "")

                if doc:
                    if dados_nf.get("tipo") == "NFSE":
                        nfse_pendencias.append(sap.pendencia_nfse(dados_nf))
                    log.info("Lançamento OK. Rodando Auditoria POP (Shadow Mode)...")
                    try:
                        aud = pop_rules.auditar_detalhado(
                            tipo_doc=dados_nf["tipo"],
                            pop_type=config_filial["pop_type"],
                            uf_empresa=config_filial["uf"],
                            uf_emitente=dados_nf["uf_emitente"],
                            itens_po=card_data.get("itens_po", []),
                            dados_fiscais_cds=resultado.get("dados_fiscais_cds", [])
                        )
                        auditorias.append((numero_nf, aud))
                        relatorio_pop_batch += f"\n[NF {numero_nf}]\n{aud['texto']}\n"
                    except Exception as e:
                        auditorias.append((numero_nf, {"status": "nao_verificado", "itens": []}))
                        relatorio_pop_batch += f"\n[NF {numero_nf}]\n⚠️ *Erro na Auditoria POP:* {str(e)}\n"
                        
                    # Item lançado num pedido indicado no XML que não estava no card (sap._carregar_pedidos_extra_xml)
                    if resultado.get("aviso_pedido_extra"):
                        relatorio_pop_batch += f"\n[NF {numero_nf}]\nℹ️ {resultado['aviso_pedido_extra']}\n"

                    # Despacha a Fase 2 (Auditoria Contas a Pagar) se houve alertas na gravação financeira
                    if alerta_financeiro:
                        msg_alerta_fin = f"🚨 *Auditoria Financeira - Passivo SAP*\n\nA Nota Fiscal {numero_nf} vinculada ao Pedido {po_number} foi lançada sob o documento contábil {doc}/{ano}.\n\n{alerta_financeiro}\n\n🔗 *Acesse o Card:* https://app.pipefy.com/open-cards/{card_id}"
                        enviar_alerta_slack(msg_alerta_fin, "Contas a Pagar", "warning")
                        relatorio_pop_batch += f"\n[Auditoria Financeira]\n{alerta_financeiro}\n"
                
                if score > maior_score:
                    maior_score = score
                    
                lista_sucessos.append(f"NF {numero_nf}: {doc}/{ano}")
                
                if card_data.get("vencimento_retroativo"):
                    msg_venc = f"🚨 *Atenção: Nota Lançada com Vencimento Retroativo*\n\nA Nota Fiscal {numero_nf} vinculada ao Pedido {po_number} acabou de ser registrada no SAP, mas a data de vencimento informada no XML do fornecedor já passou.\n\n🔗 *Acesse o Card:* https://app.pipefy.com/open-cards/{card_id}"
                    enviar_alerta_slack(msg_venc, "Contas a Pagar", "error")

            except Exception as e:
                tb = traceback.format_exc()
                erro_str = str(e)
                responsavel = responsavel_do_card(erro_str, card_data)
                
                lista_erros.append(f"[NF {numero_nf}] [{responsavel}] {erro_str}")
                
                contexto_falha = (
                    f"❌ *Falha no Lançamento Automático*\n"
                    f"• *Card ID:* {card_id}\n"
                    f"• *Pedido SAP:* {po_number}\n"
                    f"• *Nota Fiscal:* {numero_nf}\n"
                    f"• *Ação para:* {responsavel}\n"
                    f"• *Motivo do Erro:* {erro_str}"
                )
                log.error("Erro na NF %s do card %s:\n%s\n%s", numero_nf, card_id, contexto_falha, tb)
                enviar_alerta_slack(contexto_falha, responsavel, "error")

        log.info("[6/7] Registrando resultados em lote no Pipefy...")
        pipefy.registrar_lote_pipefy(card_id, lista_sucessos, lista_erros, maior_score, tipos_docs, relatorio_pop_batch)

        # Campos DIFAL, CFOP e Utilização (relatório do Contábil)
        for campo, valor in campos_fiscais(auditorias).items():
            try:
                pipefy.atualizar_campo(card_id, campo, valor)
            except Exception as e:
                log.warning("Não consegui preencher o campo %s do card %s: %s", campo, card_id, e)

        if nfse_pendencias:
            try:
                pipefy.comentar_card(card_id, "🧾 NFS-e lançada pela automação. Falta completar no SAP:\n"
                                     + "\n".join(nfse_pendencias)
                                     + "\nDepois de completar, mova o card para a próxima fase.")
            except Exception as e:
                log.warning("Card %s: não consegui comentar as pendências da NFS-e (%s).", card_id, e)

        if len(lista_erros) == 0:
            tipo_operacao = card_data.get("tipo_operacao", "normal")
            
            # --- TRAVA DE MOVIMENTAÇÃO PARA CT-E ---
            if "CTE" in tipos_docs:
                fase_proxima = ""
                log.info("[7/7] Lote contém CT-e. Mantendo card na fase atual para ajustes manuais da NF (Origem/Destino e Código Frete).")
            elif "NFSE" in tipos_docs:
                # NFS-e: como o CT-e, fica na fase para o Contas a Pagar completar a nota (Sist. geração NFS-e e
                # Nº aleatório não entram pela API). Pedido do João, 02/11/2026.
                fase_proxima = ""
                log.info("[7/7] Lote contém NFS-e. Mantendo card na fase atual para completar a nota (Sist. geração e Nº aleatório).")
            elif tipo_operacao == "cartoes":
                fase_proxima = PHASE_ID_CARTAO
                nome_fase = "Compra cartão / Validação"
            else:
                fase_proxima = os.getenv("NEXT_PHASE_ID", "")
                nome_fase = "Contábil"
                pular, motivo_contabil = decidir_pular_contabil(auditorias, nf_sem_auditoria)
                validado_auto = False
                if pular:
                    # A fase Contábil marcava "Card validado?" à mão; ao pular, a automação marca SIM.
                    try:
                        pipefy.atualizar_campo(card_id, CAMPO_CARD_VALIDADO, "SIM")
                    except Exception as e:
                        log.warning("Card %s: não consegui marcar 'Card validado?' (%s).", card_id, e)
                    recusa = pipefy.mover_card_verificado(card_id, sap.PAGAMENTO_PHASE_ID)
                    if not recusa:
                        fase_proxima = ""
                        validado_auto = True
                        log.info("[7/7] Auditoria POP OK: card %s enviado direto para Pagamento (sem a fase Contábil).", card_id)
                        try:
                            pipefy.comentar_card(card_id, "🤖 Auditoria fiscal automática OK (CFOP e impostos conferidos, "
                                                          "utilização liberada): card enviado direto para Pagamento, sem a fase Contábil.")
                        except Exception:
                            pass
                    else:
                        log.warning("Card %s: Pipefy recusou ir direto para Pagamento (%s); segue para o Contábil.", card_id, recusa)
                        motivo_contabil = f"Pipefy recusou ir direto para Pagamento ({recusa})"
                        try:
                            pipefy.atualizar_campo(card_id, CAMPO_CARD_VALIDADO, "")   # o Contábil é quem valida
                        except Exception:
                            pass
                else:
                    log.info("Card %s segue para o Contábil: %s.", card_id, motivo_contabil)
                # Marca na 1ª linha da Auditoria Fiscal (o filtro "contém" dos relatórios do Pipefy só vê o começo):
                # "VALIDADO AUTOMATICAMENTE" ou "ENVIADO AO CONTÁBIL: <motivo>".
                marcador = ("🤖 VALIDADO AUTOMATICAMENTE: card enviado direto para Pagamento, sem a fase Contábil"
                            if validado_auto else f"📋 ENVIADO AO CONTÁBIL: {motivo_contabil}")
                try:
                    pipefy.atualizar_campo(card_id, pipefy.ID_CAMPO_AUDITORIA,
                                           marcador + "\n" + pipefy.com_linha_de_ajuste(relatorio_pop_batch))
                except Exception as e:
                    log.warning("Card %s: não consegui gravar o marcador na Auditoria Fiscal (%s).", card_id, e)

            if fase_proxima:
                log.info(f"[7/7] Sucesso total no lote. Movendo card para a fase '{nome_fase}'...")
                pipefy.mover_card(card_id, fase_proxima)
        else:
            log.warning("[7/7] O lote conteve erros. O Card não será movido de fase.")

        log.info("═══ Processamento concluído — card %s ═══", card_id)

    except Exception as e_global:
        tb = traceback.format_exc()
        erro_str = str(e_global)

        if "XML da NF não encontrado" in erro_str:
            log.info("Processamento abortado (Aviso): %s", erro_str)
            try:
                pipefy.registrar_erro(card_id, erro_str, responsavel_do_card("Divergência Comercial", locals().get("card_data")), is_critical=True, apenas_marcar_erro=True)
            except Exception as err_mov:
                log.warning("Falha ao registrar aviso no Pipefy: %s", err_mov)
            return

        responsavel = responsavel_do_card(erro_str, locals().get("card_data"))
        
        contexto_falha = (
            f"❌ *Falha Crítica no Card*\n"
            f"• *Card ID:* {card_id}\n"
            f"• *Pedido SAP:* {po_number}\n"
            f"• *Ação para:* {responsavel}\n"
            f"• *Motivo do Erro:* {erro_str}"
        )
        log.error("Erro GLOBAL no card %s:\n%s\n%s", card_id, contexto_falha, tb)
        enviar_alerta_slack(contexto_falha, responsavel, "error")
        
        try: 
            pipefy.registrar_erro(card_id, erro_str, responsavel, tipo_doc="OUTROS")
        except Exception as err_mov: 
            log.warning("Falha ao registrar erro no Pipefy: %s", err_mov)

# ─────────────────────────────────────────────────────────────────────────────
#  Endpoints Flask (Webhooks e App FrontEnd)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/webhook/pipefy", methods=["POST"])
def webhook_pipefy():
    auth_header = request.headers.get("Authorization", "")
    token_esperado = f"Bearer {WEBHOOK_SECRET}"
    if auth_header != token_esperado: return jsonify({"error": "Não autorizado"}), 401

    try: evento = request.get_json(force=True)
    except Exception: return jsonify({"error": "JSON inválido"}), 400

    data = evento.get("data", {})
    if data.get("action", "") != "card.move": return jsonify({"status": "ignored"}), 200

    fase_destino_id = str(data.get("to", {}).get("id", ""))
    card_id = str(data.get("card", {}).get("id", ""))

    if not card_id: return jsonify({"error": "card_id ausente no payload"}), 400

    # Entrada na fase Pagamento: ajuste das parcelas depois da validação contábil (fase_pagamento.py).
    if sap.PAGAMENTO_PHASE_ID and fase_destino_id == sap.PAGAMENTO_PHASE_ID:
        from fase_pagamento import processar_pagamento
        threading.Thread(target=processar_pagamento, args=(card_id,), kwargs={"alertar": enviar_alerta_slack}).start()
        return jsonify({"status": "pagamento_started"}), 200

    # Entrada na fase Validação da Nota Fiscal (pipe da Logística): mesmas checagens do lançamento, sem lançar.
    if os.getenv("VALIDACAO_LOGISTICA_PHASE_ID", "").strip() and fase_destino_id == os.getenv("VALIDACAO_LOGISTICA_PHASE_ID", "").strip():
        from validacao_logistica import validar_card
        threading.Thread(target=validar_card, args=(card_id,)).start()
        return jsonify({"status": "validacao_started"}), 200

    if TRIGGER_PHASE_ID and fase_destino_id != str(TRIGGER_PHASE_ID): return jsonify({"status": "ignored"}), 200

    threading.Thread(target=processar_card, args=(card_id,)).start()
    return jsonify({"status": "processing_started"}), 200

@app.route("/processar/<card_id>", methods=["POST"])
def processar_manual(card_id: str):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if token != os.getenv("MANUAL_TRIGGER_TOKEN", ""): return jsonify({"error": "Não autorizado"}), 401
    
    processar_card(card_id)
    return jsonify({"status": "finished"}), 200

@app.route("/", methods=["GET"])
def serve_app():
    html_path = os.path.join(os.path.dirname(__file__), "app_lancamento.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/parse-xml", methods=["POST"])
def parse_xml_endpoint():
    body = request.get_json(force=True)
    try:
        dados = xml_parser.parsear(base64.b64decode(body.get("xml", "")))
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    data_fmt = ""
    if dados.get("data_emissao"):
        try:
            ms = int(dados["data_emissao"].replace("/Date(", "").replace(")/", ""))
            data_fmt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%d/%m/%Y")
        except Exception: pass

    return jsonify({**dados, "data_emissao_fmt": data_fmt}), 200

@app.route("/buscar-po/<numero_po>", methods=["GET"])
def buscar_po(numero_po: str):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if token != os.getenv("MANUAL_TRIGGER_TOKEN", ""): return jsonify({"error": "Não autorizado"}), 401

    resultado = sap.buscar_empresa_por_po(numero_po)
    if not resultado: return jsonify({"error": "PO não encontrado"}), 404
    return jsonify(resultado), 200

@app.route("/lancar", methods=["POST"])
def lancar_endpoint():
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if token != os.getenv("MANUAL_TRIGGER_TOKEN", ""): return jsonify({"error": "Não autorizado"}), 401

    body = request.get_json(force=True)
    try:
        dados_nf = xml_parser.parsear(base64.b64decode(body.get("xml", "")))
    except Exception as e:
        return jsonify({"error": f"Erro ao ler XML: {e}"}), 400

    cnpj_dest = str(dados_nf.get("cnpj_destinatario", "")).strip()
    config_filial = MAPA_FILIAIS_CNPJ.get(cnpj_dest)
    
    if not config_filial:
        return jsonify({"error": f"Nota Fiscal Inválida: Nenhum dos CNPJs envolvidos no XML pertence às nossas filiais."}), 400

    tipo_operacao_app = body.get("tipo_operacao", "normal")

    card_data = {
        "card_id":                   body.get("card_id", "app"),
        "empresa":                   config_filial.get("company_code", "BR01"),
        "numero_pedido_sap":         body.get("numero_pedido_sap", ""),
        "cond_pagamento":            body.get("cond_pagamento", ""),
        "tipo_operacao":             tipo_operacao_app,
        "texto":                     body.get("texto", ""), 
        "numero_nf_override":        body.get("numero_nf_override", ""),
        "itens_po":                  body.get("itens_po", []),
        "cnpj_fornecedor_sap":       body.get("cnpj_fornecedor_sap", ""),
        "payment_terms":             body.get("payment_terms", ""),
        "condicoes_encontradas":     body.get("condicoes_encontradas", []),
        "subir_bloqueado_pagamento": body.get("subir_bloqueado_pagamento", False)
    }
    
    po_unit = body.get("po_unit", "").strip()
    if po_unit: dados_nf["unidade_medida"] = po_unit

    try:
        resultado = sap.lancar_fatura_completo(dados_nf, card_data, config_filial)
        
        doc = resultado.get("supplier_invoice", "")
        ano = resultado.get("fiscal_year", "")
        alerta_fin = resultado.get("alerta_financeiro", "")
        
        if doc:
            try:
                msg_auditoria = pop_rules.auditar(
                    tipo_doc=dados_nf["tipo"],
                    pop_type=config_filial["pop_type"],
                    uf_empresa=config_filial["uf"],
                    uf_emitente=dados_nf["uf_emitente"],
                    itens_po=card_data.get("itens_po", []),
                    dados_fiscais_cds=resultado.get("dados_fiscais_cds", [])
                )
            except Exception as e:
                msg_auditoria = f"⚠️ *Erro na Auditoria POP:* {str(e)}"
                
            if alerta_fin:
                msg_auditoria += f"\n\n[Auditoria Financeira]\n{alerta_fin}"
            if resultado.get("aviso_pedido_extra"):
                msg_auditoria += f"\n\nℹ️ {resultado['aviso_pedido_extra']}"
                
            resultado["relatorio_pop"] = msg_auditoria
        
    except ValueError as ve:
        log.warning("Lançamento bloqueado: %s", str(ve))
        responsavel = classificar_responsavel_erro(str(ve))
        enviar_alerta_slack(f"Lançamento via App bloqueado: {str(ve)}", responsavel, "warning")
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        log.error("Erro interno fatal:\n%s", traceback.format_exc())
        responsavel = classificar_responsavel_erro(str(e))
        enviar_alerta_slack(f"Erro interno fatal (App):\n{str(e)}", responsavel, "error")
        return jsonify({"error": "Erro interno no servidor."}), 500

    card_id = body.get("card_id", "")
    if card_id and card_id != "app":
        try:
            pipefy.registrar_sucesso(card_id, resultado, dados_nf.get("tipo", "Outros"))
            
            if dados_nf.get("tipo") == "CTE":
                fase_proxima = ""
                log.info("Lançamento de CT-e realizado via App. Card mantido na fase atual para ajustes manuais.")
            elif dados_nf.get("tipo") == "NFSE":
                fase_proxima = ""
                log.info("Lançamento de NFS-e realizado via App. Card mantido na fase atual para completar a nota.")
                try:
                    pipefy.comentar_card(card_id, "🧾 NFS-e lançada pela automação. Falta completar no SAP:\n"
                                         + sap.pendencia_nfse(dados_nf)
                                         + "\nDepois de completar, mova o card para a próxima fase.")
                except Exception:
                    pass
            elif tipo_operacao_app == "cartoes":
                fase_proxima = PHASE_ID_CARTAO
            else:
                fase_proxima = os.getenv("NEXT_PHASE_ID", "")
                
            if fase_proxima: 
                pipefy.mover_card(card_id, fase_proxima)
        except Exception: pass

    return jsonify(resultado), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8009))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    log.info("Iniciando servidor na porta %d", port)
    app.run(host="0.0.0.0", port=port, debug=debug)