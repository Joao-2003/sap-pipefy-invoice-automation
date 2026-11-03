import logging
import requests
import re
import os
import sys
import time
from requests.auth import HTTPBasicAuth
from datetime import datetime, timezone
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Reaproveitando as credenciais configuradas
from config import (
    SAP_BASE_URL,
    SAP_AUTH_METHOD, SAP_USERNAME, SAP_PASSWORD,
    SAP_OAUTH_TOKEN_URL, SAP_OAUTH_CLIENT_ID, SAP_OAUTH_CLIENT_SECRET,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
log = logging.getLogger("relatorio_ncm")

# Caminhos das APIs
BASE_PO = SAP_BASE_URL.rstrip("/") + "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV"
BASE_PROD = SAP_BASE_URL.rstrip("/") + "/sap/opu/odata/sap/API_PRODUCT_SRV"

# ─────────────────────────────────────────────────────────────────────────────
#  Autenticação SAP
# ─────────────────────────────────────────────────────────────────────────────
_oauth_cache = {"token": None, "expires_at": 0}

def _get_oauth_token() -> str:
    now = time.time()
    if _oauth_cache["token"] and now < _oauth_cache["expires_at"] - 60: 
        return _oauth_cache["token"]
    resp = requests.post(SAP_OAUTH_TOKEN_URL, data={"grant_type": "client_credentials"}, 
                         auth=HTTPBasicAuth(SAP_OAUTH_CLIENT_ID, SAP_OAUTH_CLIENT_SECRET), timeout=15)
    resp.raise_for_status()
    d = resp.json()
    _oauth_cache["token"], _oauth_cache["expires_at"] = d["access_token"], now + int(d.get("expires_in", 3600))
    return d["access_token"]

def _auth_headers() -> dict: 
    headers = {"Accept": "application/json"}
    if SAP_AUTH_METHOD == "oauth2":
        headers["Authorization"] = f"Bearer {_get_oauth_token()}"
    return headers

def _auth_kwargs() -> dict: 
    return {"auth": HTTPBasicAuth(SAP_USERNAME, SAP_PASSWORD)} if SAP_AUTH_METHOD == "basic" else {}

# ─────────────────────────────────────────────────────────────────────────────
#  Função Auxiliar de Formatação de Data SAP
# ─────────────────────────────────────────────────────────────────────────────
def _formatar_data_sap(raw_date: str) -> str:
    if not raw_date:
        return ""
    str_date = str(raw_date).strip()
    
    # Formato OData /Date(123456789)/
    if "/Date(" in str_date:
        try:
            ms = int(re.search(r'-?\d+', str_date).group())
            return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%d/%m/%Y")
        except Exception:
            return str_date
            
    # Formato ISO (YYYY-MM-DD ou YYYY-MM-DDTHH:MM:SS)
    if len(str_date) >= 10 and str_date[4] == '-' and str_date[7] == '-':
        try:
            dt = datetime.strptime(str_date[:10], "%Y-%m-%d")
            return dt.strftime("%d/%m/%Y")
        except Exception:
            return str_date
            
    return str_date

# ─────────────────────────────────────────────────────────────────────────────
#  Lógica de Extração e Relatório Excel
# ─────────────────────────────────────────────────────────────────────────────
def gerar_relatorio_excel(total_desejado=100000):
    session = requests.Session()
    headers = _auth_headers()
    kwargs = _auth_kwargs()

    log.info(f"Iniciando extração de {total_desejado} itens do SAP...")
    
    itens_po = []
    skip = 0
    chunk_size = 5000  # Limite máximo de resposta do SAP
    
    # 1. Paginação para buscar todos os registros
    while skip < total_desejado:
        current_top = min(chunk_size, total_desejado - skip)
        
        # CORREÇÃO CRÍTICA: Adicionado 'to_PurchaseOrder' na lista do $select
        url_po = (f"{BASE_PO}/A_PurchaseOrderItem?$select=PurchaseOrder,PurchaseOrderItem,Material,"
                  f"Plant,ConsumptionTaxCtrlCode,PurchaseOrderItemText,to_PurchaseOrder&$expand=to_PurchaseOrder"
                  f"&$filter=Material ne ''&$top={current_top}&$skip={skip}&$orderby=PurchaseOrder desc")
        
        log.info(f"Baixando lote de pedidos do SAP (Skip: {skip} / Solicitando: {current_top})...")
        resp_po = session.get(url_po, headers=headers, **kwargs)
        resp_po.raise_for_status()
        
        resultados_lote = resp_po.json().get("d", {}).get("results", [])
        if not resultados_lote:
            log.info("Não há mais itens a serem baixados (Fim da fila).")
            break
            
        itens_po.extend(resultados_lote)
        skip += current_top
        
        if len(resultados_lote) < current_top:
            break
            
    total_baixado = len(itens_po)
    log.info(f"Total de {total_baixado} itens de pedido baixados! Cruzando NCMs com o Mestre de Materiais...")

    cache_materiais = {} 
    dados_excel = []

    # 2. Laço de Processamento
    for idx, item in enumerate(itens_po, start=1):
        if idx % 500 == 0:
            log.info(f"Processando item {idx} de {total_baixado}... (Materiais únicos em cache: {len(cache_materiais)})")
            
        po_num = item.get("PurchaseOrder", "")
        item_num = item.get("PurchaseOrderItem", "")
        material = item.get("Material", "").strip()
        descricao = item.get("PurchaseOrderItemText", "").strip()
        centro = item.get("Plant", "").strip()
        ncm_pedido = str(item.get("ConsumptionTaxCtrlCode", "")).replace(".", "").strip()
        
        # Pega a data de criação do cabeçalho expandido
        cabecalho_po = item.get("to_PurchaseOrder", {})
        raw_date = cabecalho_po.get("CreationDate", "") if isinstance(cabecalho_po, dict) else ""
        data_criacao = _formatar_data_sap(raw_date)

        chave_mat = f"{material}-{centro}"
        
        # Busca NCM no Mestre de Materiais (com Cache)
        if chave_mat not in cache_materiais:
            url_prod = f"{BASE_PROD}/A_ProductPlant(Product='{material}',Plant='{centro}')?$select=Product,Plant,ConsumptionTaxCtrlCode"
            resp_prod = session.get(url_prod, headers=headers, **kwargs)
            
            if resp_prod.ok:
                dados_prod = resp_prod.json().get("d", {})
                ncm_mestre = str(dados_prod.get("ConsumptionTaxCtrlCode", "")).replace(".", "").strip()
                cache_materiais[chave_mat] = ncm_mestre
            else:
                cache_materiais[chave_mat] = "ERRO_BUSCA"
                
        ncm_mestre = cache_materiais[chave_mat]
        
        # Comparação NCM
        if ncm_mestre == "ERRO_BUSCA":
            status_edicao = "N/A"
        elif ncm_pedido != ncm_mestre:
            status_edicao = "EDITADO"
        else:
            status_edicao = "OK"

        dados_excel.append([
            po_num, item_num, data_criacao, centro, material, 
            descricao, ncm_pedido, ncm_mestre, status_edicao
        ])

    # 3. Geração do Arquivo Excel (.xlsx)
    log.info("Montando arquivo Excel (.xlsx)...")
    wb = Workbook()
    ws = wb.active
    ws.title = "Auditoria NCM"

    cabecalhos = [
        "Pedido SAP", "Item", "Data Criação", "Centro", 
        "Material", "Descrição", "NCM no Pedido", "NCM Cadastro Mestre", "Status"
    ]
    ws.append(cabecalhos)

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4F81BD", end_color="4F81BD", fill_type="solid")
    
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for linha in dados_excel:
        ws.append(linha)

    larguras = {"A": 15, "B": 10, "C": 15, "D": 10, "E": 18, "F": 40, "G": 18, "H": 22, "I": 15}
    for col, width in larguras.items():
        ws.column_dimensions[col].width = width

    vermelho_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, min_col=1, max_col=9):
        if row[8].value == "EDITADO": 
            for cell in row:
                cell.fill = vermelho_fill

    nome_arquivo = "Relatorio_Auditoria_NCM.xlsx"
    wb.save(nome_arquivo)
    
    qtd_editados = sum(1 for linha in dados_excel if linha[8] == "EDITADO")
    total_itens = len(dados_excel)
    pct_editados = (qtd_editados / total_itens) * 100 if total_itens > 0 else 0

    log.info(f"✨ Concluído! Relatório salvo como '{nome_arquivo}'.")
    log.info(f"📊 Resumo: {qtd_editados} itens ({pct_editados:.1f}%) foram editados manualmente.")
    
    return nome_arquivo, qtd_editados, total_itens, pct_editados


def enviar_relatorio_slack(nome_arquivo, qtd_editados, total_itens, pct_editados):
    # O Client do Slack exige um token bot válido que pode subir arquivos
    slack_token = os.getenv("SLACK_BOT_TOKEN", "") 
    canal_id = os.getenv("SLACK_CHANNEL_NCM", "")

    if not slack_token:
        log.error("Token de Bot do Slack (SLACK_BOT_TOKEN) não configurado!")
        return

    client = WebClient(token=slack_token)
    
    mensagem = (
        f"Bom dia! 📊 Aqui está a *Auditoria Semanal de NCMs*.\n"
        f"Foram avaliados *{total_itens}* itens no SAP.\n"
        f"⚠️ *{qtd_editados}* itens ({pct_editados:.1f}%) estão com o NCM divergente do Mestre de Materiais."
    )

    try:
        log.info("Fazendo upload da planilha para o Slack...")
        client.files_upload_v2(
            channel=canal_id,
            file=nome_arquivo,
            title="Auditoria_NCM_Semanal.xlsx",
            initial_comment=mensagem
        )
        log.info("Relatório enviado ao Slack com sucesso!")
    except SlackApiError as e:
        log.error(f"Falha ao enviar arquivo para o Slack: {e.response['error']}")


if __name__ == "__main__":
    # Sempre gera o arquivo localmente
    arquivo, qtd, total, pct = gerar_relatorio_excel(total_desejado=100000)
    
    # Se rodado com a flag --slack, faz o upload pro canal
    if len(sys.argv) > 1 and sys.argv[1] == "--slack":
        enviar_relatorio_slack(arquivo, qtd, total, pct)