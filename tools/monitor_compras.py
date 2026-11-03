"""
monitor_compras.py — Dashboard Interno de Monitoramento de Condições de Pagamento
Estética Dark, Alinhamento Pixel-Perfect Linha a Linha, Busca em Lote de Nomes (Fornecedor e Usuário) e Scroll.
"""

import os
import sys
import requests
import pandas as pd
from io import BytesIO
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, jsonify, send_file
from slack_sdk.webhook import WebhookClient

# Importa suas configurações seguras
import sap_client
from config import SAP_BASE_URL

app = Flask(__name__)

# --- URL CORRETA DA API DE PEDIDOS NO S/4HANA ---
BASE_PO_URL = SAP_BASE_URL.rstrip("/") + "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder"

# ==============================================================================
# DICIONÁRIOS DE APOIO (CONDIÇÕES E USUÁRIOS MANUAIS)
# ==============================================================================

MAPA_USUARIOS_MANUAL = {
    "CB998877": "João Lima",
    "CB112233": "Tarley",
    # Adicione os outros compradores aqui...
}

MAPA_CONDICOES = {
    "0001": "Pagar imediatamente s/dedução",
    "0002": "14 dias 2%, 30 líquido",
    "0003": "14 dias 3%, 20/2%, 30 líq.",
    "0004": "A partir do fim do mês",
    "0005": "A partir do dia 10 do mês seg.",
    "0006": "Fim do mês 4%, meio do mês 2%",
    "0007": "15/31 mês seguinte 2%",
    "0008": "14 dias 2%, 30/1,5%, 45 líq.",
    "0009": "Pagável em 3 prestações",
    "0010": "14 dias, 20 dias, 30 dias",
    "0045": "Pagável em 45 dias",
    "0090": "Pagável em 90 dias",
    "0120": "Pagável em 120 dias",
    "0150": "Pagável em 150 dias",
    "0180": "Pagável em 180 dias",
    "A002": "A ajustar",
    "C001": "Pagável em 14/28 dias",
    "C002": "Pagável em 28/35/42 dias",
    "C003": "Pagável em 28/42/56 dias",
    "C004": "Pagável em 30/45/60 dias",
    "C005": "Pagável em 30/60/90 dias",
    "C006": "Pagável em 30/60/90/120 dias",
    "C007": "Pagável em 30/60/90/120/150 Dias",
    "C008": "Pagável em 30/60/90/120/150/180 Dias",
    "C009": "Pagável em 15/30/45/60/75/90/105 Dias",
    "C010": "Pagável em 30/45 Dias",
    "C011": "Pagável em 15/30/45 Dias",
    "C012": "Pagável em 15/30 Dias",
    "C013": "Pagável em 28/35 Dias",
    "C014": "Pagável em 28/56 Dias",
    "C015": "Pagável em 30/45/60/75 Dias",
    "C016": "Pagável em 30/45/60/75/90 DIAS",
    "CP01": "Entrada de 20% + 40% em 30/04/26 + 40% em 30/09/26",
    "CP02": "Entrada 50% em 30/09/2025 + 50% para 30/04/2026",
    "CP03": "10% 15% 30/08 25% 25% 25%",
    "CP04": "30% 30 35% 30.05.26 35% 30.09.26",
    "CP05": "E 30% 70% 30.11.25",
    "CP06": "E 30% 70% 30/03/2026",
    "CP07": "E10% 45% 30/04/26 45% 30/09/26",
    "CP08": "E10% 30% 30.09 30% 30.05.26 35% 30.08.26",
    "CP09": "E20% 40% 30.05.26 40% 30.09.26",
    "CP10": "E50% 50% 30.04.2026",
    "CP11": "50% 30.08 50% 20.09",
    "CP12": "E10% 30 25% 30.03.26",
    "CP13": "25% 30/04/26 20% 30/05/26 20% 30/06/26",
    "CP14": "Entrada 30% restante em 30/60/90 dias",
    "D001": "Vencimento líquido em 1 Dias",
    "D002": "Vencimento líquido em 2 Dias",
    "D003": "Vencimento líquido em 3 Dias",
    "D007": "Vencimento líquido em 7 Dias",
    "D010": "Vencimento líquido em 10 Dias",
    "D014": "Vencimento líquido em 14 Dias",
    "D015": "Vencimento líquido em 15 Dias",
    "D020": "Vencimento líquido em 20 Dias",
    "D021": "Vencimento líquido em 21 Dias",
    "D028": "Vencimento líquido em 28 Dias",
    "D030": "Vencimento líquido em 30 Dias",
    "D035": "Vencimento líquido em 35 Dias",
    "D042": "Vencimento líquido em 42 Dias",
    "D045": "Vencimento líquido em 45 Dias",
    "D056": "Vencimento líquido em 56 Dias",
    "D060": "Vencimento líquido em 60 Dias",
    "D075": "Vencimento líquido em 75 Dias",
    "D090": "Vencimento líquido em 90 Dias",
    "D105": "Vencimento líquido em 105 Dias",
    "D180": "dentro de 180 dias s/desconto",
    "D365": "Vencimento líquido em 365 Dias",
    "F001": "5 Dias Após o Faturamento",
    "F002": "7 Dias Após o Faturamento",
    "F003": "14 Dias Após o Faturamento",
    "F004": "15 Dias Após o Faturamento",
    "F005": "Data básica em 30 do mês seguinte / pagamento imediato s/desconto",
    "F006": "Data base no dia 30 em 2 meses / pagamento imediato s/desconto",
    "F007": "Data base no dia 30 em 3 meses / pagamento imediato s/desconto",
    "F008": "Data base no dia 30 em 4 meses / pagamento imediato s/desconto",
    "F009": "Data base no dia 30 em 5 meses / pagamento imediato s/desconto",
    "F010": "Data base no dia 30 em 6 meses / pagamento imediato s/desconto",
    "F011": "Data base no dia 30 em 7 meses / pagamento imediato s/desconto",
    "F012": "Data base no dia 30 em 8 meses / pagamento imediato s/desconto",
    "F013": "Data base no dia 30 em 9 meses / pagamento imediato s/desconto",
    "F014": "Data base no dia 30 em 10 meses / pagamento imediato s/desconto",
    "F015": "Data base no dia 30 em 11 meses / pagamento imediato s/desconto",
    "F016": "Data base no dia 30 em 12 meses / pagamento imediato s/desconto",
    "F017": "0 Dias Após o Faturamento",
    "F018": "21 Dias Após o Faturamento",
    "F019": "41 Dias Após o Faturamento",
    "F020": "63 Dias Após o Faturamento",
    "F021": "0/21/0 Após Fat/ 21/42/63 Faturamento",
    "NT00": "Pagável mediante recibo",
    "NT08": "Vencimento líquido em 8 dias",
    "NT15": "Vencimento líquido em 15 dias",
    "NT30": "Vencimento líquido em 30 dias",
    "NT45": "Vencimento líquido em 45 dias",
    "NT60": "Vencimento líquido em 60 dias",
    "P001": "Pagável em 30 dias",
    "P002": "Pagável em 60 dias",
    "P003": "Pagável em 90 dias",
    "P008": "Pagável em 30/60/90/120/150 D",
    "P012": "Pagável à vista 14/28 dias",
    "P032": "Pagamento em 12X",
    "P033": "Pagável em 120 dias",
    "P035": "Pagável em 150 dias",
    "P036": "Pagável em 180 dias",
    "P037": "Pagável em 210 dias",
    "P038": "Pagável em 240 dias",
    "P039": "Pagável em 270 dias",
    "P040": "Pagável em 300 dias",
    "P041": "Pagável em 330 dias",
    "P042": "Pagável em 360 dias",
    "P043": "Dia 30 + 04/26 - 05/26 - 06/26",
    "P046": "J15%:ENT17%+17% P/30/10/25+55%30/06/26+11%30/08/26",
    "P047": "10%ent p/30d 25%30/03/26, 25%30/05/26, 20%30/06/26",
    "P048": "50% em 30/04/26 + 50% em 30/05/26",
    "P049": "ENT30%, 35% EM 30/04/26, 35% EM 30/09/26",
    "P050": "Pagável em 15 dias",
    "P051": "Pagável em 21/28/35 dias",
    "P052": "Pg. 60/90/120/150/180 dias",
    "P060": "60/90/150/180DIAS",
    "P065": "15/30/45/60DIAS",
    "P066": "Pagável em 20/40 dias",
    "P067": "Pgto 30/45/60/75/90/105 dias",
    "PJ15": "Juros 15% + Entrada 30% + 20% 30/10 + 50% 30/04",
    "V020": "Pagável em 20 Dias",
    
    # --- Novas / Atualizadas pela TI ---
    "A000": "Pgto. antecipado",
    "A001": "Pgto. à vista",
    "E002": "Entr. 30% + 70% em 30 dias (embarque)",
    "E003": "Entr. 30% + 70% em 60 dias (embarque)",
    "E004": "Entr. 30% + 70% em 90 dias (embarque)",
    "E005": "Entr. 50% + 50% em 60 dias",
    "EP01": "Entr. 30% + 70% em 30/60/90 dias",
    "P004": "Parc. 30/60 dias",
    "P005": "Parc. 30/45/60 dias",
    "P006": "Parc. 60/90/120 dias",
    "P007": "Parc. 30/60/90/120 dias",
    "P009": "Parc. 30/60/90 dias",
    "P010": "Parc. 45/60/75 dias",
    "P011": "Parc. 45/90 dias",
    "P053": "Parc. 15/30 dias",
    "P054": "Parc. 15/30/45 dias",
    "P055": "Parc. 28/35/42 dias",
    "P056": "Parc. 28/42/56 dias",
    "V007": "Venc. em 7 dias",
    "V014": "Venc. em 14 dias",
    "V015": "Venc. em 15 dias",
    "V021": "Venc. em 21 dias",
    "V028": "Venc. em 28 dias",
    "V030": "Venc. em 30 dias",
    "V045": "Venc. em 45 dias",
    "V060": "Venc. em 60 dias",
    "V090": "Venc. em 90 dias",
    "V120": "Venc. em 120 dias",
    "V150": "Venc. em 150 dias",
    "V180": "Venc. em 180 dias"
}

def obter_nome_condicao(codigo: str) -> str:
    if not codigo or codigo == 'VAZIA / SEM CONDIÇÃO':
        return "SEM CONDIÇÃO INFORMADA NO PEDIDO"
    descricao = MAPA_CONDICOES.get(codigo, "Condição Descontinuada ou Não Mapeada")
    return f"{codigo} - {descricao}"

# ==============================================================================
# FUNÇÕES DE BUSCA EM LOTE (PERFORMANCE OTIMIZADA)
# ==============================================================================
def buscar_nomes_fornecedores(lista_ids: list) -> dict:
    """Busca em lote o nome real (BusinessPartnerFullName) dos fornecedores."""
    ids_limpos = list(set([str(i).strip().zfill(10) for i in lista_ids if str(i).strip() and str(i).strip() != "0000000000"]))
    if not ids_limpos: return {}

    mapa = {}
    session = requests.Session()
    headers = {**sap_client._auth_headers(), "Accept": "application/json"}
    kwargs = sap_client._auth_kwargs()
    base_bp = SAP_BASE_URL.rstrip("/") + "/sap/opu/odata/sap/API_BUSINESS_PARTNER"

    for i in range(0, len(ids_limpos), 20):
        lote = ids_limpos[i:i+20]
        filtro = " or ".join([f"BusinessPartner eq '{bp}'" for bp in lote])
        url = f"{base_bp}/A_BusinessPartner?$format=json&$select=BusinessPartner,BusinessPartnerFullName&$filter={filtro}"
        
        try:
            r = session.get(url, headers=headers, **kwargs, timeout=20)
            if r.ok:
                results = r.json().get("d", {}).get("results", [])
                for res in results:
                    bp_id = str(res.get("BusinessPartner")).lstrip("0")
                    nome = str(res.get("BusinessPartnerFullName", ""))
                    mapa[bp_id] = nome
        except Exception as e:
            print(f"Erro ao buscar lote de fornecedores: {e}")
            
    return mapa

def buscar_nomes_usuarios(lista_ids: list) -> dict:
    """Busca em lote o nome real dos usuários do SAP (CreatedByUser) via Custom CDS View."""
    ids_limpos = list(set([str(i).strip() for i in lista_ids if str(i).strip()]))
    if not ids_limpos: return {}
    
    mapa = {}
    session = requests.Session()
    headers = {**sap_client._auth_headers(), "Accept": "application/json"}
    kwargs = sap_client._auth_kwargs()
    
    base_url = SAP_BASE_URL.rstrip("/") + "/sap/opu/odata/sap/YY1_BUSUSERAPI_CDS"
    
    for i in range(0, len(ids_limpos), 20):
        lote = ids_limpos[i:i+20]
        filtro = " or ".join([f"UserID eq '{uid}'" for uid in lote])
        url = f"{base_url}/YY1_BusUserAPI?$format=json&$select=UserID,PersonFullName&$filter={filtro}"
        
        try:
            r = session.get(url, headers=headers, **kwargs, timeout=10)
            if r.ok:
                for res in r.json().get("d", {}).get("results", []):
                    uid = str(res.get("UserID", ""))
                    nome = str(res.get("PersonFullName", ""))
                    if uid and nome: mapa[uid] = nome
            else:
                print(f"Erro na API de Usuários: {r.text}")
        except Exception as e:
            print(f"Erro ao buscar lote de usuários: {e}")
            
    return mapa

def formatar_nome_usuario_fallback(userid: str) -> str:
    """Fallback: Tenta limpar o formato se não encontrar na API nem no dicionário."""
    u_str = str(userid).strip()
    if "." in u_str and "@" not in u_str:
        return u_str.replace(".", " ").title()
    return u_str

def buscar_pedidos_sap(data_inicio: str, data_fim: str, purch_group: str = "") -> list:
    """Bate na API do SAP e traz cabeçalhos e itens numa cajadada só."""
    session = requests.Session()
    headers = {**sap_client._auth_headers(), "Accept": "application/json"}
    kwargs = sap_client._auth_kwargs()

    filtro_data = f"CreationDate ge datetime'{data_inicio}T00:00:00' and CreationDate le datetime'{data_fim}T23:59:59'"
    
    # Aplica o filtro de grupo de compras (Suporta Exclusão do G04)
    if purch_group:
        if purch_group == "NOT_G04":
            filtro_data += " and PurchasingGroup ne 'G04'"
        else:
            filtro_data += f" and PurchasingGroup eq '{purch_group}'"

    params = {
        "$format": "json",
        "$select": "PurchaseOrder,CompanyCode,Supplier,PaymentTerms,CreationDate,CreatedByUser,PurchasingGroup,to_PurchaseOrderItem/NetPriceAmount,to_PurchaseOrderItem/OrderQuantity,to_PurchaseOrderItem/NetPriceQuantity",
        "$expand": "to_PurchaseOrderItem",
        "$filter": filtro_data,
        "$top": 5000
    }

    resp = session.get(BASE_PO_URL, headers=headers, params=params, **kwargs, timeout=60)
    resp.raise_for_status()
    
    pedidos_raw = resp.json().get("d", {}).get("results", [])
    if not pedidos_raw:
        return []
        
    todos_fornecedores_ids = [p.get("Supplier") for p in pedidos_raw]
    todos_usuarios_ids = [p.get("CreatedByUser") for p in pedidos_raw]
    
    mapa_forn = buscar_nomes_fornecedores(todos_fornecedores_ids)
    mapa_users = buscar_nomes_usuarios(todos_usuarios_ids)
    
    pedidos_processados = []
    for p in pedidos_raw:
        valor_total_pedido = 0.0
        itens = p.get("to_PurchaseOrderItem", {}).get("results", [])
        
        for item in itens:
            qty = float(item.get("OrderQuantity", 0))
            net_price = float(item.get("NetPriceAmount", 0))
            price_unit = float(item.get("NetPriceQuantity", 1) or 1)
            if price_unit > 0:
                valor_total_pedido += (qty / price_unit) * net_price
                
        raw_supplier = p.get("Supplier", "")
        clean_sup_id = raw_supplier.lstrip("0") if raw_supplier else "INTERNO"
        
        raw_user = p.get("CreatedByUser", "SISTEMA")
        nome_user = mapa_users.get(raw_user) or MAPA_USUARIOS_MANUAL.get(raw_user) or formatar_nome_usuario_fallback(raw_user)
        
        pedidos_processados.append({
            "PurchaseOrder": p.get("PurchaseOrder"),
            "SupplierID": clean_sup_id,
            "SupplierName": mapa_forn.get(clean_sup_id, "Fornecedor Interno"),
            "PaymentTerms": p.get("PaymentTerms", "VAZIA / SEM CONDIÇÃO"),
            "CreatedByUser": raw_user,
            "UserName": nome_user,
            "ValorTotal": valor_total_pedido
        })

    return pedidos_processados


# ==============================================================================
# DISPARO DO RELATÓRIO SEMANAL VIA SLACK
# ==============================================================================
def enviar_relatorio_semanal_slack():
    print("Iniciando rotina de envio do relatório para o Slack...")
    webhook_url = os.getenv("SLACK_WEBHOOK_COMPRAS")
    if not webhook_url:
        print("Erro: A variável SLACK_WEBHOOK_COMPRAS não está configurada no .env!")
        return

    hoje = datetime.now()
    data_fim = hoje - timedelta(days=1)
    data_inicio = hoje - timedelta(days=8)

    start_str = data_inicio.strftime("%Y-%m-%d")
    end_str = data_fim.strftime("%Y-%m-%d")
    start_br = data_inicio.strftime("%d/%m/%Y")
    end_br = data_fim.strftime("%d/%m/%Y")

    try:
        # Busca EXCLUINDO o G04 para a visão geral limpa no Slack
        pedidos = buscar_pedidos_sap(start_str, end_str, "NOT_G04")
        if not pedidos:
            msg = f"Bom dia, time de Compras! ☕\nNenhum pedido de compra foi gerado entre {start_br} e {end_br}."
            WebhookClient(webhook_url).send(text=msg)
            print("Slack enviado (Sem pedidos).")
            return

        df = pd.DataFrame(pedidos)
        df['PaymentTerms'] = df['PaymentTerms'].replace('', 'VAZIA / SEM CONDIÇÃO')
        df['Condicao_Exibicao'] = df['PaymentTerms'].apply(obter_nome_condicao)

        resumo = df.groupby('Condicao_Exibicao').size().reset_index(name='Quantidade')
        resumo = resumo.sort_values(by='Quantidade', ascending=False)
        total_pedidos = len(df)

        texto_slack = (
            f"Bom dia, time de Compras! ☕\n"
            f"Aqui está o Raio-X das Condições de Pagamento utilizadas nos últimos 7 dias (*{start_br}* a *{end_br}*):\n"
            f"_(Nota: Operações do Grupo G04 / Cartões foram excluídas desta visão)_\n\n"
            f"🛒 *Total de Pedidos Emitidos:* {total_pedidos}\n\n"
            f"🏆 *Top Condições Utilizadas:*\n"
        )

        for idx, row in resumo.head(10).iterrows():
            pct = (row['Quantidade'] / total_pedidos) * 100
            alerta = " 🚨" if "vista" in row['Condicao_Exibicao'].lower() or "antecipado" in row['Condicao_Exibicao'].lower() else ""
            label_ped = "pedido" if row['Quantidade'] == 1 else "pedidos"
            texto_slack += f"• *{row['Condicao_Exibicao']}*: {row['Quantidade']} {label_ped} ({pct:.1f}%){alerta}\n"

        texto_slack += f"\n👉 <http://localhost:5000/monitor-compras/|Acesse o Monitor de Compras para ver o relatório e auditoria completa>"

        webhook = WebhookClient(webhook_url)
        response = webhook.send(text=texto_slack)
        
        if response.status_code == 200:
            print(f"Relatório do período {start_br} a {end_br} enviado ao Slack com sucesso!")
        else:
            print(f"Erro na API do Slack: {response.body}")
            
    except Exception as e:
        print(f"Erro fatal ao processar relatório do Slack: {e}")


# ==============================================================================
# ROTAS FLASK (DASHBOARD)
# ==============================================================================
@app.route("/api/dados")
def api_dados():
    start = request.args.get('start')
    end = request.args.get('end')
    purch_group = request.args.get('group', '')
    
    if not start or not end:
        return jsonify({"error": "Datas não informadas"}), 400
        
    try:
        pedidos = buscar_pedidos_sap(start, end, purch_group)
        
        if not pedidos:
            return jsonify({"resumo": [], "total": 0, "valor_total": 0.0})
            
        df = pd.DataFrame(pedidos)
        
        df['CreatedByUser'] = df['CreatedByUser'].fillna('SISTEMA')
        df['UserName'] = df['UserName'].fillna('SISTEMA')
        df['SupplierID'] = df['SupplierID'].fillna('INTERNO')
        df['SupplierName'] = df['SupplierName'].fillna('INTERNO')
        
        df['PaymentTerms'] = df['PaymentTerms'].replace('', 'VAZIA / SEM CONDIÇÃO')
        df['Condicao_Exibicao'] = df['PaymentTerms'].apply(obter_nome_condicao)
        
        resumo = []
        total_pedidos = len(df)
        valor_total_periodo = float(df['ValorTotal'].sum()) if not df.empty else 0.0
        
        for cond, group in df.groupby(['PaymentTerms', 'Condicao_Exibicao']):
            qtd = len(group)
            valor_total = group['ValorTotal'].sum()
            
            all_users = group.groupby(['CreatedByUser', 'UserName'], dropna=False).agg(
                Qtd=('PurchaseOrder', 'count'),
                Valor=('ValorTotal', 'sum')
            ).reset_index().sort_values('Valor', ascending=False).to_dict('records')
            
            all_suppliers = group.groupby(['SupplierID', 'SupplierName'], dropna=False).agg(
                Qtd=('PurchaseOrder', 'count'),
                Valor=('ValorTotal', 'sum')
            ).reset_index().sort_values('Valor', ascending=False).to_dict('records')
            
            resumo.append({
                "PaymentTerms": cond[0],
                "Condicao_Exibicao": cond[1],
                "Quantidade": qtd,
                "Percentual": round((qtd / total_pedidos) * 100, 1) if total_pedidos > 0 else 0,
                "ValorTotal": valor_total,
                "AllUsers": all_users,
                "AllSuppliers": all_suppliers
            })
            
        resumo = sorted(resumo, key=lambda x: x['Quantidade'], reverse=True)
        return jsonify({"resumo": resumo, "total": total_pedidos, "valor_total": valor_total_periodo})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/export-excel")
def exportar_excel():
    start = request.args.get('start')
    end = request.args.get('end')
    purch_group = request.args.get('group', '')
    
    try:
        pedidos = buscar_pedidos_sap(start, end, purch_group)
        df = pd.DataFrame(pedidos)
        
        if '__metadata' in df.columns:
            df = df.drop(columns=['__metadata'])
            
        df['PaymentTerms'] = df['PaymentTerms'].replace('', 'VAZIA / SEM CONDIÇÃO')
        df['Descrição da Condição'] = df['PaymentTerms'].apply(obter_nome_condicao)
        df = df.rename(columns={
            "PurchaseOrder": "Pedido SAP",
            "SupplierID": "ID Fornecedor",
            "SupplierName": "Nome Fornecedor",
            "CreatedByUser": "ID Criador",
            "UserName": "Nome Criador",
            "ValorTotal": "Valor Total do Pedido (R$)"
        })
        
        resumo = df.groupby('Descrição da Condição').agg(
            Qtd_Pedidos=('Pedido SAP', 'count'),
            Valor_Total=('Valor Total do Pedido (R$)', 'sum')
        ).reset_index().sort_values(by='Qtd_Pedidos', ascending=False)
        
        resumo['% de Uso'] = ((resumo['Qtd_Pedidos'] / len(df)) * 100).round(1).astype(str) + '%'
        
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            resumo.to_excel(writer, index=False, sheet_name='Ranking Condições')
            cols = ['Pedido SAP', 'ID Criador', 'Nome Criador', 'ID Fornecedor', 'Nome Fornecedor', 'PaymentTerms', 'Descrição da Condição', 'Valor Total do Pedido (R$)']
            df[cols].to_excel(writer, index=False, sheet_name='Base Completa SAP')
        output.seek(0)
        
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f'Auditoria_Compras_{start}_{end}.xlsx'
        )
    except Exception as e:
        return f"Erro ao gerar Excel: {e}", 500


@app.route("/")
def dashboard():
    html_template = """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Monitor de Compras</title>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;600&family=JetBrains+Mono:wght@300;400;600;700&display=swap" rel="stylesheet">
    <style>
    * { margin:0; padding:0; box-sizing:border-box; }
    :root {
      --bg:#0c1810; --sur:#101e14; --card:#141f17; --card2:#192419;
      --bdr:#1d2f20; --bdr2:#253828;
      --G:#3ecf6e; --A:#f5a623; --R:#f06060; --B:#5ba4cf;
      --t:#dff0e4; --t2:#8aab94; --t3:#4a6652;
      --FH:'Playfair Display',Georgia,serif; --FM:'JetBrains Mono',monospace;
    }
    body { background:var(--bg); color:var(--t); font-family:var(--FM); font-size:12px; padding-bottom:40px; }
    ::-webkit-scrollbar { width:6px; height:6px; }
    ::-webkit-scrollbar-track { background:var(--bg); }
    ::-webkit-scrollbar-thumb { background:var(--t3); border-radius:3px; }
    
    #hdr { display:flex; align-items:center; justify-content:space-between; padding:16px 28px; border-bottom:1px solid var(--bdr); background:linear-gradient(90deg,#0c1810,#0f2015 50%,#0c1810); position:sticky; top:0; z-index:300; gap:20px; flex-wrap:wrap; }
    .brand-wrap { display:flex; align-items:center; gap:16px; }
    .brand-logo { height:48px; width:auto; object-fit:contain; filter:drop-shadow(0px 2px 4px rgba(0,0,0,0.5)); background-color: #fff; padding: 4px; border-radius: 6px;}
    .brand h1 { font-family:var(--FH); font-size:1.4rem; font-weight:600; color:var(--B); margin-bottom:2px;}
    .brand p { font-size:10px; color:var(--t2); letter-spacing:1px; text-transform:uppercase; }
    
    .filters { display:flex; align-items:center; gap:12px; background:var(--card2); padding:8px 16px; border-radius:6px; border:1px solid var(--bdr2); flex-wrap:wrap; margin-left: auto; }
    .flbl { font-size:10px; color:var(--t2); text-transform:uppercase; letter-spacing:1px; }
    
    select, input[type="date"] { background:var(--sur); border:1px solid var(--bdr2); color:var(--t); font-family:var(--FM); font-size:11px; padding:4px 8px; border-radius:4px; outline:none; color-scheme:dark;}
    .btn { background:var(--B); color:#0c1810; border:none; padding:6px 12px; font-family:var(--FM); font-weight:700; font-size:10px; border-radius:4px; cursor:pointer; text-transform:uppercase; transition:0.2s; text-decoration:none; display:inline-flex; align-items:center; gap:4px; }
    .btn:hover { background:var(--t); }
    .btn-excel { background:#1f7244; color:#fff; }
    .btn-excel:hover { background:#288c54; color:#fff; }
    
    #main { padding:24px 28px; }
    .ks { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:20px; }
    .kp { background:var(--card); border:1px solid var(--bdr); border-radius:6px; padding:18px; position:relative; overflow:hidden; }
    .kp::before { content:''; position:absolute; top:0; left:0; right:0; height:3px; background:linear-gradient(90deg, transparent, var(--B), transparent); }
    .kl { font-size:10px; letter-spacing:1.2px; text-transform:uppercase; color:var(--t2); margin-bottom:8px; }
    .kv { font-family:var(--FH); font-size:2.2rem; font-weight:600; color:var(--t); line-height:1; }
    
    /* Layout Split Direto no Flex */
    .g-split { display:flex; gap:20px; align-items:flex-start; }
    .col-esq { flex: 1; max-width: calc(50% - 10px); }
    .col-dir { flex: 1; max-width: calc(50% - 10px); position: sticky; top: 100px;}
    
    .c { background:var(--card); border:1px solid var(--bdr); border-radius:6px; overflow:hidden;}
    .ct { font-size:11px; letter-spacing:1px; text-transform:uppercase; color:var(--t); padding: 18px 18px 0 18px; margin-bottom:16px; display:flex; align-items:center; gap:8px; font-weight:600; }
    .ct::before { content:''; width:4px; height:12px; background:var(--B); border-radius:2px; }
    
    .sync-row { border-bottom: 1px solid var(--bdr); transition: background 0.2s; display:flex; flex-direction:column; padding:14px 18px; cursor:pointer;}
    .sync-row:hover, .sync-row.active { background:rgba(91,164,207,0.1); border-left: 3px solid var(--B); padding-left: 15px;}
    
    .table-container { max-height: 250px; overflow-y: auto; border-bottom: 1px solid var(--bdr2); }
    .vt { width:100%; border-collapse:collapse; font-size:11px; }
    .vt thead th { position: sticky; top: 0; height: 30px; padding:0 12px; text-align:left; font-size:9px; text-transform:uppercase; color:var(--t2); background:var(--card2); border-top:1px solid var(--bdr2); border-bottom:1px solid var(--bdr2); font-weight:bold; z-index:10;}
    .vt td { height: 35px; padding:0 12px; border-bottom:1px solid var(--bdr); color:var(--t); vertical-align:middle; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
    .vt tbody tr:hover { background:rgba(255,255,255,0.02); }
    
    .loading { display:none; color: var(--A); font-weight: bold; font-size: 14px; margin-bottom: 15px;}
    
    @media (max-width: 900px) {
        .g-split { flex-direction: column; }
        .col-esq, .col-dir { max-width: 100%; flex: 1; position: relative; top: 0; }
        .ks { grid-template-columns: 1fr; max-width: 100%; }
    }
    </style>
    </head>
    <body>

    <div id="hdr">
      <div class="brand-wrap">
        
        <div class="brand">
          <h1>Monitor de Compras</h1>
          <p>Auditoria de Condições de Pagamento S/4HANA</p>
        </div>
      </div>
      
      <div class="filters">
        <a href="#" class="btn btn-excel" onclick="baixarXlsx(event)">📥 Baixar XLSX (Auditoria Completa)</a>
        
        <span class="flbl">Grupo:</span>
        <select id="cbGroup">
            <option value="">Todos</option>
            <option value="NOT_G04">Todos (Exceto G04)</option>
            <option value="G01">G01</option>
            <option value="G02">G02</option>
            <option value="G03">G03</option>
            <option value="G04">G04 (Administrativo/Cartões)</option>
        </select>
        
        <span class="flbl">Criado Em:</span>
        <input type="date" id="dtStart">
        <span style="color:var(--t3)">até</span>
        <input type="date" id="dtEnd">
        
        <button class="btn" onclick="buscarDados()">Pesquisar SAP</button>
      </div>
    </div>

    <div id="main">
      <div id="loading" class="loading">⏳ Consultando OData SAP S/4HANA (Buscando Fornecedores e Valores)... Aguarde.</div>
      
      <div class="ks">
        <div class="kp"><div class="kl">Total de Pedidos Gerados (Período)</div><div class="kv" id="kpiTotal">0</div></div>
        <div class="kp"><div class="kl">Valor Total dos Pedidos (Período)</div><div class="kv" id="kpiValorTotal" style="color: var(--G);">R$ 0,00</div></div>
      </div>

      <div class="g-split">
        <div class="col-esq c">
          <div class="ct">Ranking de Uso das Condições</div>
          <div id="cssChart" style="display: flex; flex-direction: column; overflow-y:auto; max-height: 600px;"></div>
        </div>
        
        <div class="col-dir c" id="detailCard" style="display:none; min-height: 600px;">
          <div class="ct" id="detTitle" style="color:var(--B);">Detalhes da Condição</div>
          <div style="padding: 0 18px 20px 18px;">
              <div style="font-size: 26px; font-family: var(--FH); font-weight: bold; color: var(--G);" id="detValue"></div>
              <div style="font-size: 11px; color: var(--t2); margin-bottom: 24px;">Volume Financeiro Transacionado</div>
              
              <div style="display:grid; grid-template-columns: 1fr; gap:20px;">
                  <div>
                      <div style="color:var(--B); font-weight:bold; margin-bottom:8px; text-transform:uppercase; font-size:10px;">👤 Todos os Compradores</div>
                      <div class="table-container">
                          <table class="vt" id="tUsers">
                              <thead><tr><th>Comprador</th><th style="text-align:center; width:80px;">Pedidos</th><th style="text-align:right; width:120px;">Total (R$)</th></tr></thead>
                              <tbody></tbody>
                          </table>
                      </div>
                  </div>
                  
                  <div>
                      <div style="color:var(--A); font-weight:bold; margin-bottom:8px; text-transform:uppercase; font-size:10px;">🏭 Todos os Fornecedores</div>
                      <div class="table-container">
                          <table class="vt" id="tSuppliers">
                              <thead><tr><th>Fornecedor</th><th style="text-align:center; width:80px;">Pedidos</th><th style="text-align:right; width:120px;">Total (R$)</th></tr></thead>
                              <tbody></tbody>
                          </table>
                      </div>
                  </div>
              </div>
          </div>
        </div>
        
        <div class="col-dir c" id="emptyCard" style="min-height: 600px; display:flex; align-items:center; justify-content:center; color:var(--t3); font-size: 14px; font-style:italic;">
          👈 Clique em uma condição no gráfico para auditar os compradores e fornecedores.
        </div>
        
      </div>
    </div>

    <script>
    const formatter = new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' });
    let globalResumo = [];

    const hoje = new Date().toISOString().split('T')[0];
    document.getElementById('dtStart').value = hoje;
    document.getElementById('dtEnd').value = hoje;

    async function buscarDados() {
        const start = document.getElementById('dtStart').value;
        const end = document.getElementById('dtEnd').value;
        const group = document.getElementById('cbGroup').value;
        
        if(!start || !end) return alert("Selecione as datas!");
        
        document.getElementById('loading').style.display = 'block';
        document.getElementById('detailCard').style.display = 'none';
        document.getElementById('emptyCard').style.display = 'flex';
        
        try {
            const res = await fetch(`api/dados?start=${start}&end=${end}&group=${group}`);
            const data = await res.json();
            
            if(data.error) {
                alert("Erro SAP: " + data.error);
                return;
            }
            
            document.getElementById('kpiTotal').innerText = data.total;
            document.getElementById('kpiValorTotal').innerText = formatter.format(data.valor_total || 0);
            globalResumo = data.resumo;
            renderBlocks(data.resumo);
            
        } catch(e) {
            alert("Erro de comunicação com o servidor.");
        } finally {
            document.getElementById('loading').style.display = 'none';
        }
    }

    function renderBlocks(resumo) {
        const chartDiv = document.getElementById('cssChart');
        chartDiv.innerHTML = '';
        
        if(resumo.length === 0) {
            chartDiv.innerHTML = '<div style="padding:20px; text-align:center; color:var(--t2);">Nenhum pedido encontrado.</div>';
            return;
        }
        
        let maxQtd = Math.max(...resumo.map(r => r.Quantidade));
        
        resumo.forEach((r, idx) => {
            let widthPct = maxQtd > 0 ? (r.Quantidade / maxQtd * 100).toFixed(1) : 0;
            let valFormatado = formatter.format(r.ValorTotal);
            let labelPedido = r.Quantidade === 1 ? "Pedido" : "Pedidos";
            
            chartDiv.innerHTML += `
            <div class="sync-row" id="row-${idx}" onclick="showDetails(${idx})">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
                    <div style="font-weight:bold; color:var(--t); font-size:12px;">${r.Condicao_Exibicao}</div>
                    <div style="font-size:12px; color:var(--G); font-weight:bold; text-align:right;">${valFormatado}</div>
                </div>
                <div style="display:flex; align-items:center; gap:10px;">
                    <div style="flex-grow:1; height:12px; background:var(--bdr); border-radius:4px; overflow:hidden;">
                        <div style="width: ${widthPct}%; height:100%; background:var(--B); border-radius:4px;"></div>
                    </div>
                    <div style="width: 80px; text-align: right; font-size: 11px; font-weight: bold; color: var(--t2);">
                        ${r.Quantidade} ${labelPedido}
                    </div>
                </div>
            </div>`;
        });
    }

    function showDetails(index) {
        document.querySelectorAll('.sync-row').forEach(r => r.classList.remove('active'));
        document.getElementById(`row-${index}`).classList.add('active');
        
        document.getElementById('emptyCard').style.display = 'none';
        document.getElementById('detailCard').style.display = 'block';
        
        const data = globalResumo[index];
        document.getElementById('detTitle').innerText = data.Condicao_Exibicao;
        document.getElementById('detValue').innerText = formatter.format(data.ValorTotal);
        
        let tU = document.querySelector('#tUsers tbody');
        tU.innerHTML = '';
        data.AllUsers.forEach(u => {
            let fullName = u.UserName && u.UserName !== 'SISTEMA' ? u.UserName : u.CreatedByUser;
            let hoverText = `${u.CreatedByUser} - ${fullName}`;
            tU.innerHTML += `<tr>
                <td title="${hoverText}" style="max-width: 180px;">${fullName}</td>
                <td style="text-align:center">${u.Qtd}</td>
                <td style="text-align:right; color:var(--G); font-weight:bold;">${formatter.format(u.Valor)}</td>
            </tr>`;
        });
        
        let tS = document.querySelector('#tSuppliers tbody');
        tS.innerHTML = '';
        data.AllSuppliers.forEach(s => {
            let fullName = s.SupplierName && s.SupplierName !== 'INTERNO' ? s.SupplierName : s.SupplierID;
            let hoverText = `${s.SupplierID} - ${fullName}`;
            tS.innerHTML += `<tr>
                <td title="${hoverText}" style="max-width: 180px;">${fullName}</td>
                <td style="text-align:center">${s.Qtd}</td>
                <td style="text-align:right; color:var(--G); font-weight:bold;">${formatter.format(s.Valor)}</td>
            </tr>`;
        });
    }

    function baixarXlsx(e) {
        e.preventDefault();
        const start = document.getElementById('dtStart').value;
        const end = document.getElementById('dtEnd').value;
        const group = document.getElementById('cbGroup').value;
        if(!start || !end) return alert("Selecione as datas antes de baixar!");
        window.location.href = `export-excel?start=${start}&end=${end}&group=${group}`;
    }
    
    buscarDados();
    </script>
    </body>
    </html>
    """
    return render_template_string(html_template)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--slack":
        enviar_relatorio_semanal_slack()
    else:
        port = int(os.getenv("PORT_COMPRAS", 8012))
        print(f"🚀 Monitor de Compras rodando na porta {port}")
        app.run(host="0.0.0.0", port=port, debug=True)