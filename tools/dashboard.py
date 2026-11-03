"""
dashboard.py — Dashboard C-Level de Eficiência da Automação de Notas Fiscais a empresa
Estética Dark, Logo Oficial via CDN, Exportação XLSX, Filtros Dinâmicos (NFS / Falta de XML / Período).
Busca em tempo real direto na Memória RAM (com destruição total de cache HTTP).
"""

import os
import re
import time
import requests
import json
import unicodedata
from io import BytesIO
import pandas as pd
from flask import Flask, render_template_string, request, send_file, jsonify

app = Flask(__name__)

# --- Configurações do Pipefy ---
PIPEFY_API_URL = "https://api.pipefy.com/graphql"
PIPEFY_TOKEN = os.getenv("PIPEFY_TOKEN", "")
PIPE_ID = int(os.getenv("PIPEFY_PIPE_ID", "0"))
REPORT_ID = int(os.getenv("PIPEFY_REPORT_ID", "0"))
# Relatório "Auditoria Fiscal contém ✅ Contas a Pagar:" (cards com ajuste automático de parcelas).
# Só é baixado se o relatório principal não tiver a coluna Auditoria Fiscal.
REPORT_ID_AJUSTES = int(os.getenv("PIPEFY_REPORT_ID_AJUSTES", "0"))
PREFIXO_AJUSTE_CP = "✅ Contas a Pagar:"

HEADERS = {
    "Authorization": f"Bearer {PIPEFY_TOKEN}",
    "Content-Type": "application/json"
}

# --- BLINDAGEM ANTI-CACHE HTTP ---
@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route("/health")
def health_check():
    return jsonify({"status": "ok", "timestamp": time.time()}), 200

def baixar_relatorio_pipefy(report_id=REPORT_ID):
    print(f"[{time.strftime('%H:%M:%S')}] Pedindo relatório {report_id} em tempo real ao Pipefy...")
    mutation = f"""
    mutation {{
      exportPipeReport(input: {{pipeId: {PIPE_ID}, pipeReportId: {report_id}}}) {{
        pipeReportExport {{ id }}
      }}
    }}
    """
    resp = requests.post(PIPEFY_API_URL, json={"query": mutation}, headers=HEADERS).json()
    
    if "errors" in resp: 
        raise Exception(f"Erro na API do Pipefy: {resp['errors']}")
    if "data" not in resp:
        raise Exception(f"O Pipefy recusou a conexão! Resposta exata do Pipefy: {resp}")
        
    export_id = resp["data"]["exportPipeReport"]["pipeReportExport"]["id"]
    
    query_status = f"""
    query {{ pipeReportExport(id: "{export_id}") {{ state fileURL }} }}
    """
    for _ in range(15):
        time.sleep(2)
        resp_status = requests.post(PIPEFY_API_URL, json={"query": query_status}, headers=HEADERS).json()
        estado = resp_status["data"]["pipeReportExport"]["state"]
        url_arquivo = resp_status["data"]["pipeReportExport"]["fileURL"]
        
        if estado == "done" and url_arquivo:
            print("Download concluído! Processando dados em memória RAM...")
            url_sem_cache = f"{url_arquivo}&_t={int(time.time())}" if "?" in url_arquivo else f"{url_arquivo}?_t={int(time.time())}"
            dados_binarios = requests.get(url_sem_cache).content
            return pd.read_excel(BytesIO(dados_binarios))
            
    raise Exception("O Pipefy excedeu o tempo limite para geração do relatório.")

def marcar_parcela_ajustada(df):
    """True no card com ajuste automático de parcelas: linha "✅ Contas a Pagar: ..." no campo Auditoria Fiscal
    (gravada pela automação). Usa a coluna do relatório principal; sem ela, o relatório REPORT_ID_AJUSTES."""
    col = next((c for c in df.columns if 'auditoria fiscal' in str(c).lower()), None)
    if col:
        return df[col].astype(str).str.contains(PREFIXO_AJUSTE_CP, regex=False)
    try:
        aj = baixar_relatorio_pipefy(REPORT_ID_AJUSTES)
        col_cod = next((c for c in aj.columns if str(c).strip().lower() == 'código'), None)
        ids = set(aj[col_cod].astype(str).str.replace(r'\.0$', '', regex=True)) if col_cod else set()
    except Exception as e:
        print(f"[aviso] relatório de parcelas ajustadas indisponível: {e}")
        ids = set()
    return df['Card_Id'].astype(str).isin(ids)

def obter_dados_limpos():
    return baixar_relatorio_pipefy()


def _norm(txt):
    return unicodedata.normalize('NFKD', str(txt)).encode('ascii', 'ignore').decode().lower().strip()


def classificar_status_e_responsavel(df):
    status_lista, responsavel_lista, erro_padronizado_lista = [], [], []
    
    for idx, row in df.iterrows():
        n_sap = str(row.get('Nº Documento SAP', ''))
        erro_bruto = str(row.get('Detalhes do Erro', '')).lower()
        label = str(row.get('Etiquetas', '')).lower()
        
        if n_sap not in ['nan', '', 'None']:
            status, resp, erro_padro = 'SUCESSO', 'Automação', 'Sucesso Automático'
        elif 'xml da nf não encontrado' in erro_bruto or 'falta de xml' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Solicitante / Pipefy', 'Falta de XML no Card'
        elif 'ainda não está liberado' in erro_bruto or 'nao esta liberado' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Compras', 'Pedido de Compra Bloqueado/Não Liberado'
        elif 'bloqueio de filial' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Compras', 'Bloqueio de Filial (CNPJ NF x Centro Logístico SAP)'
        elif 'comercial' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Compras', 'Condição de Pagto Ausente/Divergente'
        elif 'pedido de compra' in erro_bruto or 'po não foi' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Solicitante / Compras', 'PO não preenchido no Pipefy'
        elif 'conta do imposto' in erro_bruto or 'ycoa' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'TI', 'Conta de Imposto Não Determinada'
        elif 'inconsist' in erro_bruto or 'compat' in erro_bruto or 'dispon' in erro_bruto or 'não há mais itens' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Compras', 'Sem Match de Item/Valor no PO'
        elif 'ncm' in erro_bruto:
            status, resp = 'FALHA', 'Compras'
            erro_padro = 'Divergência NCM (Matéria-Prima)' if 'matéria-prima' in erro_bruto or 'materia_prima' in label else 'Divergência NCM (Uso/Consumo)'
        elif 'incompatibilidade de pre' in erro_bruto or 'preço' in erro_bruto or 'preco' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Compras / Financeiro', 'Divergência de Preço (Nota > Pedido)'
        elif 'condições de pagamento' in erro_bruto or 'condicoes' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Compras', 'Condição de Pagto Ausente/Divergente'
        elif 'imposto' in erro_bruto or 'tax code' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Compras', 'Tax Code Ausente no PO'
        elif 'entrada de mercadorias' in erro_bruto or 'referencedocument' in erro_bruto or 'migo' in erro_bruto:
            if 'já criada' in erro_bruto or 'ja criada' in erro_bruto:
                 status, resp, erro_padro = 'FALHA', 'Logística e Financeiro', 'MIGO Já Vinculada a Outra NF'
            else:
                 status, resp, erro_padro = 'FALHA', 'Logística e Financeiro', 'MIGO Ausente (Rec. Físico pendente)'
        elif 'unidade' in erro_bruto or 'regras para converter' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Excelência Operacional', 'Unidade de Medida Incompatível'
        elif 'company code' in erro_bruto or 'local de negó' in erro_bruto or 'parametrização' in erro_bruto or 'parametrizacao' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Excelência Operacional', 'Parâmetro de Filial/Empresa Ausente'
        elif 'pop da empresa' in erro_bruto or 'cfop' in erro_bruto or 'finalidade' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Excelência Operacional', 'Regra Fiscal / POP Não Encontrado'
        elif 'antifraude' in erro_bruto or 'cnpj emissor' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Financeiro', 'Bloqueio Antifraude (CNPJ Divergente)'
        elif 'nota fiscal inv' in erro_bruto or 'nenhum dos cnpjs' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Financeiro', 'CNPJ Destinatário Inválido'
        elif 'períodos contábeis' in erro_bruto or 'periodos contabeis' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Financeiro / Fiscal', 'Período Contábil Fechado'
        elif 'saldo não é igual' in erro_bruto or 'saldo nao' in erro_bruto or 'ipi' in erro_bruto or 'icms' in erro_bruto or 'auto-corre' in erro_bruto or 'sistêmica' in erro_bruto or 'sistemica' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Financeiro / Fiscal', 'Divergência de Saldo Fiscal / Impostos'
        elif 'já está processando' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Financeiro', 'Pedido Bloqueado em Processamento (SAP)'
        elif 'existe' in erro_bruto or 'impossível lançar' in erro_bruto or 'impossivel lancar' in erro_bruto or 'já criada' in erro_bruto or 'ja criada' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Financeiro', 'Nota Já Lançada / Duplicidade'
        elif 'cca' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Compras', 'Custo de Aquisição Mal Definido'
        elif 'revfatem' in erro_bruto:
            status, resp, erro_padro = 'FALHA', 'Compras', 'Flag de Recebimento Ausente'
        else:
            status, resp, erro_padro = 'FALHA', 'Outros', 'Erro Genérico / Análise Manual'
            
        status_lista.append(status)
        responsavel_lista.append(resp)
        erro_padronizado_lista.append(erro_padro)
        
    df['Status_Final'] = status_lista
    df['Area_Responsavel'] = responsavel_lista
    df['Motivo_Raiz'] = erro_padronizado_lista

    # Serviço: tipo de solicitação "Serviços" OU tipo de documento de serviço ("Serviço NFS"). Todo serviço, com ou
    # sem XML (há XML aleatório anexado em card de serviço só porque o anexo é obrigatório).
    col_doc = next((c for c in df.columns if 'tipo de documento' in str(c).lower()), None)
    col_sol = next((c for c in df.columns if 'tipo de solici' in str(c).lower()), None)
    vazio = pd.Series('', index=df.index)
    doc = (df[col_doc] if col_doc else vazio).fillna('').astype(str).map(_norm)
    sol = (df[col_sol] if col_sol else vazio).fillna('').astype(str).map(_norm)
    df['Is_Servico'] = sol.str.contains('servic', na=False) | doc.str.contains('servic', na=False) | (doc == 'nfse')
    
    col_entrada = next((c for c in df.columns if 'entrou na fase notas fiscais' in str(c).lower()), None)
    if col_entrada and not df[col_entrada].isnull().all():
        df['Data_Raw'] = pd.to_datetime(df[col_entrada], errors='coerce')
    else:
        df['Data_Raw'] = pd.to_datetime(df.get('Criado em', pd.Series()), errors='coerce')
        
    df['Date'] = df['Data_Raw'].dt.strftime('%Y-%m-%d').fillna('')
    df = df[df['Date'] >= '2026-07-08'].copy()
    
    return df.fillna("")

FASES_PENDENTES = ("NOTAS FISCAIS COM PEDIDO", "PENDENTES")


def _filtro_pendentes(df):
    """Cards ainda não lançados que deram erro: na fase 'Notas Fiscais com Pedido' ou 'Pendentes',
    sem nº de documento SAP e com 'Detalhes do Erro' preenchido."""
    col_fase = next((c for c in df.columns if str(c).strip().lower() == 'fase atual'), None)
    if not col_fase:
        return df.iloc[0:0]
    fase = df[col_fase].astype(str).str.strip().str.upper()
    erro = df.get('Detalhes do Erro', pd.Series('', index=df.index)).astype(str).str.strip()
    return df[fase.isin(FASES_PENDENTES) & (df['Status_Final'] == 'FALHA') & (erro != '') & (erro.str.lower() != 'nan')]


@app.route("/export-excel")
def exportar_excel():
    df = obter_dados_limpos()
    df = classificar_status_e_responsavel(df)
    
    dt_start = request.args.get('dtStart')
    dt_end = request.args.get('dtEnd')
    hide_nfs = request.args.get('hideNfs') == 'true'
    hide_xml = request.args.get('hideXml') == 'true'
    hide_dupes = request.args.get('hideDupes') == 'true'
    card_validado = request.args.get('cardValidado')
    situacao = request.args.get('situacao')
    
    col_doc = next((c for c in df.columns if 'tipo de documento' in c.lower()), 'Qual o tipo de documento fiscal?')
    col_val = next((c for c in df.columns if 'card validado' in str(c).lower()), 'Card validado?')
    
    if dt_start and dt_end:
        df = df[(df['Date'] >= dt_start) & (df['Date'] <= dt_end)]
    if hide_nfs:
        df = df[~df['Is_Servico'].astype(bool)]
    if hide_xml:
        df = df[df['Motivo_Raiz'] != 'Falta de XML no Card']
    if hide_dupes:
        df = df[df['Motivo_Raiz'] != 'Nota Já Lançada / Duplicidade']
    if card_validado and card_validado != 'TODOS' and col_val in df.columns:
        df = df[df[col_val].astype(str).str.upper() == card_validado.upper()]
    if situacao == 'PENDENTES':
        df = _filtro_pendentes(df)
        
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Relatorio_Automacao')
    output.seek(0)
    
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='Relatorio_Automacao_Notas.xlsx'
    )

# ─────────────────────────────────────────────────────────────────────────────
#  Histórico de erros: campo "Histórico de Erros" do card (a automação acrescenta cada erro e nunca
#  apaga; o "Detalhes do Erro" guarda só o erro atual e é limpo quando o card é lançado).
#  Uma linha por erro: "dd/mm/aaaa HH:MM | <erro>". Vem no mesmo relatório do Pipefy.
# ─────────────────────────────────────────────────────────────────────────────
RE_LINHA_HIST = re.compile(r"^\s*(\d{2}/\d{2}/\d{4} \d{2}:\d{2})\s*\|\s*(.+?)\s*$")


def montar_historico_erros(df):
    """Uma linha por ocorrência de erro, a partir do df já classificado (Status_Final etc.)."""
    col_hist = next((c for c in df.columns if str(c).strip().lower() == 'histórico de erros'), None)
    if not col_hist:
        return []
    linhas = []
    for _, row in df.iterrows():
        texto = str(row.get(col_hist, '') or '')
        if not texto.strip() or texto.lower() == 'nan':
            continue
        for l in texto.splitlines():
            m = RE_LINHA_HIST.match(l)
            if not m:
                continue
            erro = m.group(2)
            nf = re.search(r"\[NF ([^\]]+)\]", erro)
            linhas.append({
                "Nº Documento SAP": "", "Detalhes do Erro": erro, "Etiquetas": row.get('Etiquetas', ''),
                "Criado em": pd.to_datetime(m.group(1), format='%d/%m/%Y %H:%M', errors='coerce'),
                "Hora": m.group(1), "Card_Id": row.get('Card_Id', ''), "Titulo": row.get('Titulo', ''),
                "Fase": row.get('Fase', ''), "NF": nf.group(1) if nf else "",
                "Card_Lancado": "SIM" if row.get('Status_Final') == 'SUCESSO' else "NÃO",
                "Servico_Card": bool(row.get('Is_Servico', False)),
            })
    if not linhas:
        return []
    h = pd.DataFrame(linhas)
    h["_ordem"] = h["Criado em"].astype(str)
    h = classificar_status_e_responsavel(h)   # mesma classificação (área / motivo) da visão geral
    h = h.sort_values("_ordem", ascending=False)
    h["Erro"] = h["Detalhes do Erro"].astype(str).str.slice(0, 400)
    cols = ["Date", "Hora", "Card_Id", "Titulo", "Fase", "NF", "Area_Responsavel", "Motivo_Raiz", "Card_Lancado", "Erro",
            "Servico_Card"]
    return h[cols].to_dict(orient="records")


@app.route("/")
def dashboard():
    try:
        df = obter_dados_limpos()
    except Exception as e:
        return f"<h1 style='color:white; background:#0c1810; padding:20px;'>Erro ao carregar dados do Pipefy: {e}</h1>"
        
    df = classificar_status_e_responsavel(df)
    
    col_tipo_solic = next((c for c in df.columns if 'tipo de solici' in c.lower()), 'Tipo de solicitação')
    col_tipo_doc = next((c for c in df.columns if 'tipo de documento' in c.lower()), 'Qual o tipo de documento fiscal?')
    col_val = next((c for c in df.columns if 'card validado' in str(c).lower()), 'Card validado?')
    
    col_fase = next((c for c in df.columns if str(c).strip().lower() == 'fase atual'), None)
    col_codigo = next((c for c in df.columns if str(c).strip().lower() == 'código'), None)
    col_titulo = next((c for c in df.columns if str(c).strip().lower() == 'título'), None)

    df = df.rename(columns={col_tipo_solic: 'Tipo_Sol', col_tipo_doc: 'Tipo_Doc', col_val: 'Card_Validado'})
    df['Fase'] = df[col_fase].astype(str) if col_fase else ''
    df['Card_Id'] = df[col_codigo].astype(str).str.replace(r'\.0$', '', regex=True) if col_codigo else ''
    df['Titulo'] = df[col_titulo].astype(str) if col_titulo else ''
    df['Erro'] = df['Detalhes do Erro'].astype(str) if 'Detalhes do Erro' in df.columns else ''
    df['Parcela_Ajustada'] = marcar_parcela_ajustada(df).astype(bool)

    df_json = df[['Date', 'Status_Final', 'Area_Responsavel', 'Motivo_Raiz', 'Tipo_Doc', 'Tipo_Sol', 'Etiquetas', 'Card_Validado',
                  'Fase', 'Card_Id', 'Titulo', 'Erro', 'Parcela_Ajustada', 'Is_Servico']].to_dict(orient='records')
    records_json = json.dumps(df_json)
    try:
        hist_json = json.dumps({"registros": montar_historico_erros(df), "gerado_em": time.strftime('%d/%m/%Y %H:%M')}).replace("</", "<\\/")
    except Exception as e:
        hist_json = json.dumps({"registros": [], "erro": str(e)})

    html_template = """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Automação Fiscal</title>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;600&family=JetBrains+Mono:wght@300;400;600;700&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0"></script>
    <style>
    * { margin:0; padding:0; box-sizing:border-box; }
    :root {
      --bg:#0c1810; --sur:#101e14; --card:#141f17; --card2:#192419;
      --bdr:#1d2f20; --bdr2:#253828;
      --G:#3ecf6e; --A:#f5a623; --R:#f06060; --B:#5ba4cf;
      --t:#dff0e4; --t2:#8aab94; --t3:#4a6652;
      --FH:'Playfair Display',Georgia,serif; --FM:'JetBrains Mono',monospace;
    }
    body { background:var(--bg); color:var(--t); font-family:var(--FM); font-size:12px; overflow-x:hidden; padding-bottom:40px; }
    ::-webkit-scrollbar { width:6px; height:6px; }
    ::-webkit-scrollbar-track { background:var(--bg); }
    ::-webkit-scrollbar-thumb { background:var(--t3); border-radius:3px; }
    
    #hdr { display:flex; align-items:center; justify-content:space-between; padding:16px 28px; border-bottom:1px solid var(--bdr); background:linear-gradient(90deg,#0c1810,#0f2015 50%,#0c1810); position:sticky; top:0; z-index:300; gap:20px; flex-wrap:wrap; }
    
    .brand-wrap { display:flex; align-items:center; gap:16px; }
    .brand-logo { height:48px; width:auto; object-fit:contain; filter:drop-shadow(0px 2px 4px rgba(0,0,0,0.5)); background-color: #fff; padding: 4px; border-radius: 6px;}
    .brand h1 { font-family:var(--FH); font-size:1.4rem; font-weight:600; color:var(--G); margin-bottom:2px;}
    .brand p { font-size:10px; color:var(--t2); letter-spacing:1px; text-transform:uppercase; }
    
    .filters { display:flex; align-items:center; gap:12px; background:var(--card2); padding:8px 16px; border-radius:6px; border:1px solid var(--bdr2); flex-wrap:wrap; margin-left: auto; }
    .flbl { font-size:10px; color:var(--t2); text-transform:uppercase; letter-spacing:1px; }
    
    .chk-group { display:flex; align-items:center; gap:6px; cursor:pointer; font-size:11px; color:var(--t); background:var(--sur); padding:4px 8px; border-radius:4px; border:1px solid var(--bdr2); user-select:none; }
    .chk-group input[type="checkbox"] { accent-color:var(--G); cursor:pointer; }
    
    .select-group { background:var(--sur); border:1px solid var(--bdr2); color:var(--t); font-family:var(--FM); font-size:11px; padding:4px 8px; border-radius:4px; outline:none; }
    
    input[type="date"] { background:var(--sur); border:1px solid var(--bdr2); color:var(--t); font-family:var(--FM); font-size:11px; padding:4px 8px; border-radius:4px; outline:none; color-scheme:dark;}
    .btn { background:var(--G); color:#0c1810; border:none; padding:6px 12px; font-family:var(--FM); font-weight:700; font-size:10px; border-radius:4px; cursor:pointer; text-transform:uppercase; transition:0.2s; text-decoration:none; display:inline-flex; align-items:center; gap:4px; }
    .btn:hover { background:var(--t); }
    .btn-excel { background:#1f7244; color:#fff; }
    .btn-excel:hover { background:#288c54; color:#fff; }
    .btn-reset { background:transparent; color:var(--t2); border:1px solid var(--bdr2); }
    .btn-reset:hover { border-color:var(--t); color:var(--t); background:transparent;}
    
    #main { padding:24px 28px; }
    .ks { display:grid; grid-template-columns:repeat(4, 1fr); gap:16px; margin-bottom:20px; }
    .kp { background:var(--card); border:1px solid var(--bdr); border-radius:6px; padding:18px; position:relative; overflow:hidden; transition: transform 0.2s, border-color 0.2s; }
    .kp:hover { transform: translateY(-2px); border-color:var(--bdr2); }
    .kp::before { content:''; position:absolute; top:0; left:0; right:0; height:3px; background:linear-gradient(90deg, transparent, var(--kc, var(--G)), transparent); }
    .kp.a { --kc:var(--A); } .kp.r { --kc:var(--R); } .kp.b { --kc:var(--B); }
    .kl { font-size:10px; letter-spacing:1.2px; text-transform:uppercase; color:var(--t2); margin-bottom:8px; }
    .kv { font-family:var(--FH); font-size:2.2rem; font-weight:600; color:var(--t); line-height:1; }
    
    .grid-45-55 { display:grid; grid-template-columns:45fr 55fr; gap:16px; margin-bottom:16px; }
    .grid-55-45 { display:grid; grid-template-columns:55fr 45fr; gap:16px; margin-bottom:16px; }
    
    /* MÁGICA DO GRID: min-width: 0 impede o estouro da tela quando há um item com rolagem horizontal interna */
    .c { background:var(--card); border:1px solid var(--bdr); border-radius:6px; padding:18px; display:flex; flex-direction:column; min-width: 0; }
    .ct { font-size:11px; letter-spacing:1px; text-transform:uppercase; color:var(--t); margin-bottom:16px; display:flex; align-items:center; gap:8px; font-weight:600; }
    .ct::before { content:''; width:4px; height:12px; background:var(--G); border-radius:2px; }
    .c.r .ct::before { background:var(--R); }
    .cw { position:relative; width:100%; flex-grow: 1; }
    
    .vt { width:100%; border-collapse:collapse; font-size:11px; table-layout: fixed; }
    .vt th { font-size:9px; text-transform:uppercase; color:var(--t2); padding:8px; text-align:left; border-bottom:1px solid var(--bdr2); background:var(--card2); }
    .vt td { padding:8px; border-bottom:1px solid var(--bdr); color:var(--t); }
    .vt tr:hover { background:rgba(255,255,255,0.02); }
    .vt td.tag-lbl { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-weight: 600; }
    .spk { height:5px; background:var(--bdr2); border-radius:2.5px; min-width:40px; display:flex; overflow:hidden;}
    .spkf { height:100%; background:var(--R); }
    .spks { height:100%; background:var(--G); }
    
    .prow { display:flex; align-items:center; gap:12px; padding:8px 0; border-bottom:1px solid var(--bdr); }
    .plbl { flex: 0 0 45%; font-size:11px; color:var(--t); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .pbw { flex:1; height:16px; background:var(--bdr); border-radius:3px; overflow:hidden; }
    .pbf { height:100%; border-radius:3px; background:var(--R); display:flex; align-items:center; justify-content:flex-end; padding-right:5px; }
    .pv { flex: 0 0 55px; text-align:right; font-size:12px; color:var(--t); font-weight:bold; line-height: 1.2; }
    .ppct { font-size:10px; color:var(--R); font-weight:bold; display:block; }

    /* Abas */
    #tabs { display:flex; gap:4px; padding:12px 28px 0; border-bottom:1px solid var(--bdr); }
    .tab { background:transparent; color:var(--t2); border:1px solid transparent; border-bottom:none; padding:8px 16px; font-family:var(--FM); font-size:11px; text-transform:uppercase; letter-spacing:1px; cursor:pointer; border-radius:6px 6px 0 0; }
    .tab.on { background:var(--card); color:var(--G); border-color:var(--bdr); font-weight:700; }
    .hist-msg { color:var(--t2); padding:30px; text-align:center; }

    /* Estilização Centralizada do Funil */
    #funnelContainer { 
        display:flex; flex-direction:column; justify-content:space-evenly; 
        height: 100%; min-height: 230px; padding: 10px 0; gap: 8px;
    }
    .funnel-bar { 
        margin: 0 auto;
        display: flex; flex-direction: column; align-items: center; justify-content: center;
        border-radius: 4px; padding: 12px; color: #fff;
        box-shadow: 0 4px 6px rgba(0,0,0,0.3);
        transition: width 0.5s ease-in-out;
        white-space: nowrap;
    }
    .funnel-title { 
        font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; 
        margin-bottom: 4px; text-shadow: 1px 1px 2px rgba(0,0,0,0.5); 
    }
    .funnel-metrics { 
        display: flex; align-items: baseline; gap: 8px; text-shadow: 1px 1px 2px rgba(0,0,0,0.5); 
    }
    .funnel-val { font-size: 16px; font-weight: bold; font-family: var(--FH); }
    .funnel-pct { font-size: 10px; opacity: 0.9; }

    /* Estilização Scroll Evolução Diária */
    .scroll-wrapper {
        width: 100%; overflow-x: auto; overflow-y: hidden; flex-grow: 1;
        display: flex; flex-direction: column; /* o gráfico ocupa toda a altura do card (acompanha o funil) */
        scrollbar-width: thin; scrollbar-color: var(--t3) var(--bg);
    }
    .scroll-wrapper::-webkit-scrollbar { height: 8px; }
    .scroll-wrapper::-webkit-scrollbar-track { background: var(--bg); border-radius:4px; }
    .scroll-wrapper::-webkit-scrollbar-thumb { background: var(--t3); border-radius: 4px; }
    </style>
    </head>
    <body>

    <div id="hdr">
      <div class="brand-wrap">
        
        <div class="brand">
          <h1>Automação de Notas Fiscais</h1>
          <p>Painel de Eficiência Operacional & Compliance S/4HANA</p>
        </div>
      </div>
      
      <div class="filters">
        <a href="#" class="btn btn-excel" onclick="baixarXlsx(event)">📥 Baixar XLSX</a>
        
        <select id="selValidado" class="select-group" onchange="applyFilter()">
          <option value="TODOS">Validação: Todos</option>
          <option value="SIM">Validação: SIM</option>
          <option value="NÃO">Validação: NÃO</option>
        </select>
        
        <select id="selSituacao" class="select-group" onchange="applyFilter()" title="Pendentes: cards em 'Notas Fiscais com Pedido' ou 'Pendentes', ainda não lançados e com erro">
          <option value="TODOS">Situação: Todos</option>
          <option value="PENDENTES">Situação: Só pendentes</option>
        </select>

        <label class="chk-group" title="Oculta todos os cards de serviço (tipo de solicitação Serviços ou documento de serviço), com ou sem XML">
          <input type="checkbox" id="chkHideNfs" onchange="applyFilter()"> Ocultar NFS
        </label>
        
        <label class="chk-group" title="Oculta erros onde o analista não subiu o XML no card">
          <input type="checkbox" id="chkHideXml" onchange="applyFilter()" checked> Ocultar S/ XML
        </label>
        
        <label class="chk-group" title="Oculta notas que o SAP já havia lançado anteriormente">
          <input type="checkbox" id="chkHideDupes" onchange="applyFilter()"> Ocultar Já Lançadas
        </label>

        <span class="flbl">Período:</span>
        <input type="date" id="dtStart">
        <span style="color:var(--t3)">até</span>
        <input type="date" id="dtEnd">
        
        <button class="btn" onclick="applyFilter()">Analisar</button>
        <button class="btn btn-reset" onclick="resetFilter()">Resetar</button>
      </div>
    </div>

    <div id="tabs">
      <button class="tab on" id="tabGeral" onclick="trocarAba('geral')">Visão Geral</button>
      <button class="tab" id="tabHist" onclick="trocarAba('hist')">Histórico de Erros</button>
    </div>

    <div id="histMain" style="display:none; padding:24px 28px;">
      <div id="histStatus" class="hist-msg">Carregando...</div>
      <div id="histConteudo" style="display:none;">
        <div class="ks">
          <div class="kp r"><div class="kl">Ocorrências de Erro</div><div class="kv" id="hKpiOc">0</div></div>
          <div class="kp b"><div class="kl">Cards Afetados</div><div class="kv" id="hKpiCards">0</div></div>
          <div class="kp"><div class="kl">Já Lançados (após erro)</div><div class="kv" id="hKpiDepois">0</div></div>
          <div class="kp a"><div class="kl">Ainda Sem Lançamento</div><div class="kv" id="hKpiSem">0</div></div>
        </div>
        <div class="grid-55-45">
          <div class="c r">
            <div class="ct">Ocorrências por Área</div>
            <div class="cw" style="height:260px"><canvas id="hArea"></canvas></div>
          </div>
          <div class="c r">
            <div class="ct">Motivos (Pareto de Ocorrências)</div>
            <div id="hPareto" style="overflow-y:auto; height:260px; padding-right:10px;"></div>
          </div>
        </div>
        <div class="c r" style="margin-bottom:16px;">
          <div class="ct">Ocorrências por Dia</div>
          <div class="cw" style="height:220px"><canvas id="hTrend"></canvas></div>
        </div>
        <div class="c r" style="margin-bottom:16px;">
          <div class="ct">Todas as Ocorrências <span id="hGerado" style="font-size:9px; color:var(--t2); text-transform:none; font-weight:400; letter-spacing:0;"></span></div>
          <div style="overflow-x:auto; max-height:520px; overflow-y:auto;">
            <table class="vt" id="tHist">
              <thead><tr>
                <th style="width: 11%;">Data</th>
                <th style="width: 16%;">Card</th>
                <th style="width: 7%;">NF</th>
                <th style="width: 11%;">Área</th>
                <th style="width: 15%;">Motivo</th>
                <th style="width: 8%; text-align:center;">Card lançado?</th>
                <th style="width: 32%;">Detalhe</th>
              </tr></thead>
              <tbody></tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <div id="main">
      <div class="ks">
        <div class="kp b"><div class="kl">Total Processado</div><div class="kv" id="kpiTotal">0</div></div>
        <div class="kp"><div class="kl">Sucesso no SAP</div><div class="kv" id="kpiSuc">0</div></div>
        <div class="kp r"><div class="kl">Falhas / Análise Manual</div><div class="kv" id="kpiErr">0</div></div>
        <div class="kp a"><div class="kl">Straight-Through Processing</div><div class="kv" id="kpiStp">0%</div></div>
      </div>

      <!-- PRIMEIRA LINHA: Funil (45%) e Evolução (55%) -->
      <div class="grid-45-55">
        <div class="c">
          <div class="ct">Funil de Conversão Operacional</div>
          <div id="funnelContainer"></div>
        </div>
        
        <div class="c">
          <div class="ct">Evolução Diária do Processamento (Deslize para ver histórico)</div>
          
          <!-- LEGENDA CLONE DO CHART.JS -->
          <div style="display:flex; justify-content:center; gap:16px; margin-bottom:16px;">
              <div style="display:flex; align-items:center; gap:6px;">
                  <span style="width:25px; height:8px; background:#3ecf6e; display:inline-block;"></span>
                  <span style="font-size:12px; color:var(--t2);">Sucesso</span>
              </div>
              <div style="display:flex; align-items:center; gap:6px;">
                  <span style="width:25px; height:8px; background:#f06060; display:inline-block;"></span>
                  <span style="font-size:12px; color:var(--t2);">Falha</span>
              </div>
          </div>
          
          <div class="scroll-wrapper" id="trendScrollContainer">
              <div class="cw" id="trendCanvasWrapper" style="min-height:250px;"><canvas id="cTrend"></canvas></div>
          </div>
        </div>
      </div>

      <!-- SEGUNDA LINHA: 55/45 -->
      <div class="grid-55-45">
        <div class="c r">
          <div class="ct">Gargalos Operacionais (Por Área) <span style="font-size:9px; color:var(--t2); text-transform:none; font-weight:400; letter-spacing:0;">(clique numa área para filtrar o Pareto)</span></div>
          <div class="cw" style="height:240px"><canvas id="cArea"></canvas></div>
        </div>
        <div class="c r">
          <div class="ct"><span>Análise de Causa Raiz dos Erros (Pareto)</span><span id="areaFilterBadge" style="display:none; font-size:10px; color:var(--R); text-transform:none; font-weight:400; letter-spacing:0; margin-left:auto; cursor:pointer; border:1px solid var(--bdr2); border-radius:10px; padding:2px 8px;" title="Clique para limpar o filtro de área"></span></div>
          <div id="paretoRoot" style="overflow-y:auto; height:240px; padding-right:10px;"></div>
        </div>
      </div>

      <!-- TERCEIRA LINHA: 45/55 -->
      <div class="grid-45-55">
        <div class="c">
          <div class="ct">Performance por Tipo de Documento Fiscal</div>
          <div class="cw" style="height:220px"><canvas id="cDocType"></canvas></div>
        </div>
        <div class="c">
          <div class="ct">Performance por Finalidade (Top 7 Solicitações)</div>
          <div class="cw" style="height:220px"><canvas id="cReqType"></canvas></div>
        </div>
      </div>

      <!-- QUARTA LINHA: 100% -->
      <div class="c" style="margin-bottom:16px;">
        <div class="ct">Análise de Risco por Etiqueta (Tags Pipefy)</div>
        <div style="overflow-x:auto;">
          <table class="vt" id="tTags">
            <thead>
              <tr>
                <th style="width: 15%;">Etiqueta</th>
                <th style="width: 12%; text-align:center;">Total</th>
                <th style="width: 12%; text-align:center;">Sucesso</th>
                <th style="width: 12%; text-align:center;">Falha</th>
                <th style="width: 49%;">Proporção</th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

      <!-- QUINTA LINHA: cards pendentes (não lançados, com erro) -->
      <div class="c r" style="margin-bottom:16px;">
        <div class="ct">Cards Pendentes com Erro <span id="pendCount" style="font-size:10px; color:var(--R); text-transform:none; font-weight:400; letter-spacing:0;"></span>
          <span style="font-size:9px; color:var(--t2); text-transform:none; font-weight:400; letter-spacing:0;">(fases Notas Fiscais com Pedido / Pendentes, sem lançamento no SAP)</span></div>
        <div style="overflow-x:auto; max-height:420px; overflow-y:auto;">
          <table class="vt" id="tPend">
            <thead>
              <tr>
                <th style="width: 18%;">Card</th>
                <th style="width: 12%;">Fase</th>
                <th style="width: 8%; text-align:center;">Dias</th>
                <th style="width: 12%;">Área</th>
                <th style="width: 18%;">Motivo</th>
                <th style="width: 32%;">Detalhe do erro</th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

    </div>

    <script>
    Chart.register(ChartDataLabels);
    
    const rawData = {{ records_json | safe }};
    Chart.defaults.color = '#8aab94';
    Chart.defaults.font.family = 'JetBrains Mono';
    
    Chart.defaults.layout.padding = { left: 15, right: 15, bottom: 5, top: 5 };
    const GR = { color:'#1d2f20', drawBorder: false };

    const stackedTooltip = {
        backgroundColor: 'rgba(16,30,20,0.95)', titleColor: '#dff0e4', bodyColor: '#8aab94', borderColor: '#253828', borderWidth: 1, padding: 10,
        callbacks: {
            label: function(ctx) {
                let v = ctx.raw;
                let total = 0;
                ctx.chart.data.datasets.forEach(ds => { total += ds.data[ctx.dataIndex] || 0; });
                let pct = total > 0 ? (v / total * 100).toFixed(1) : 0;
                return ` ${ctx.dataset.label}: ${v} (${pct}%)`;
            }
        }
    };

    const singleTooltip = {
        backgroundColor: 'rgba(16,30,20,0.95)', titleColor: '#dff0e4', bodyColor: '#8aab94', borderColor: '#253828', borderWidth: 1, padding: 10,
        callbacks: {
            label: function(ctx) {
                let v = ctx.raw;
                let total = ctx.dataset.data.reduce((a, b) => a + b, 0);
                let pct = total > 0 ? (v / total * 100).toFixed(1) : 0;
                return ` Falhas: ${v} (${pct}%)`;
            }
        }
    };

    const legendConfig = { position:'top', align:'center', labels:{color:'#8aab94', boxWidth:25, boxHeight:8} };

    let charts = {};
    let selectedArea = null;
    function mk(id, cfg) {
        if(charts[id]) charts[id].destroy();
        charts[id] = new Chart(document.getElementById(id), cfg);
    }

    // fim do período: hoje (ou a última data do relatório, se for maior) -- o histórico de erros
    // pode ter ocorrências mais novas que a última entrada de card na fase
    function fimPadrao(ultima) {
        let d = new Date(); let hoje = new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
        return (ultima && ultima > hoje) ? ultima : hoje;
    }

    function initFilter() {
        if(rawData.length > 0) {
            let dates = rawData.map(r => r.Date).filter(d => d);
            dates.sort();
            document.getElementById('dtStart').value = dates[0] || '';
            document.getElementById('dtEnd').value = fimPadrao(dates[dates.length-1]);
        }
        applyFilter();
    }
    
    function resetFilter() {
        selectedArea = null;
        document.getElementById('chkHideNfs').checked = false;
        document.getElementById('chkHideXml').checked = true;   // padrão: "Ocultar S/ XML" marcado
        document.getElementById('chkHideDupes').checked = false;
        document.getElementById('selValidado').value = 'TODOS';
        document.getElementById('selSituacao').value = 'TODOS';
        
        if(rawData.length > 0) {
            let dates = rawData.map(r => r.Date).filter(d => d);
            dates.sort();
            document.getElementById('dtStart').value = dates[0] || '';
            document.getElementById('dtEnd').value = fimPadrao(dates[dates.length-1]);
        }
        applyFilter();
    }

    const funnelColors = [
        // Degradê do azul ao verde, em 5 tons (um por linha do funil)
        '#1e6091', // Azul
        '#1b7886', // Azul Esverdeado
        '#18907a', // Verde Azulado
        '#14a86e', // Verde Água
        '#11c063'  // Verde (Parcela Ajustada)
    ];

    const FASES_PENDENTES = ['NOTAS FISCAIS COM PEDIDO', 'PENDENTES'];
    function ehPendente(r) {
        let fase = String(r.Fase || '').trim().toUpperCase();
        let erro = String(r.Erro || '').trim();
        return FASES_PENDENTES.includes(fase) && r.Status_Final === 'FALHA' && erro !== '' && erro.toLowerCase() !== 'nan';
    }
    function esc(t) {
        return String(t || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    function applyFilter() {
        const start = document.getElementById('dtStart').value;
        const end = document.getElementById('dtEnd').value;
        const hideNfs = document.getElementById('chkHideNfs').checked;
        const hideXml = document.getElementById('chkHideXml').checked;
        const hideDupes = document.getElementById('chkHideDupes').checked;
        const selValidado = document.getElementById('selValidado').value;
        
        let dateFiltered = rawData;
        if(start && end) {
            dateFiltered = rawData.filter(r => r.Date >= start && r.Date <= end || !r.Date); 
        }

        // --- CÁLCULO DO FUNIL ---
        let f1 = dateFiltered.length;
        
        // Base elegível = bruto menos os MESMOS 3 filtros das caixas "Ocultar NFS", "Ocultar S/ XML" e "Ocultar Já
        // Lançadas" (= KPI Total Processado com as 3 marcadas). Serviço = todo card de serviço, com ou sem XML.
        let step2Data = dateFiltered.filter(r => {
            let isServ = r.Is_Servico === true;
            let isXml = r.Motivo_Raiz === 'Falta de XML no Card';
            let isDupe = r.Motivo_Raiz === 'Nota Já Lançada / Duplicidade';
            return !isServ && !isXml && !isDupe;
        });
        let f2 = step2Data.length;
        
        let step3Data = step2Data.filter(r => r.Status_Final === 'SUCESSO');
        let f3 = step3Data.length;
        
        let step4Data = step3Data.filter(r => {
            let val = String(r.Card_Validado || '').trim().toUpperCase();
            return val === 'SIM';
        });
        let f4 = step4Data.length;

        // Parcela ajustada automaticamente pela automação (linha "✅ Contas a Pagar:" na Auditoria Fiscal),
        // entre os lançados com sucesso; o % "Anterior" é sobre a Contabilidade Validada.
        let step5Data = step3Data.filter(r => r.Parcela_Ajustada === true);
        let f5 = step5Data.length;

        const renderFunnelRow = (label, val, prevVal, totalVal, isFirst, idx, prevLabel = 'Anterior') => {
            let pctTotal = totalVal > 0 ? (val / totalVal * 100).toFixed(1) : 0;
            let pctPrev = prevVal > 0 ? (val / prevVal * 100).toFixed(1) : 0;
            
            let pctHtml = isFirst ? '100% do Total' : `${pctTotal}% do Total | ${pctPrev}% ${prevLabel}`;
            
            // Largura estritamente matemática. O piso invisível de 15 impede a quebra visual se for 0%.
            let mathematicalRatio = totalVal > 0 ? (val / totalVal * 100) : 0;
            let visualWidth = Math.max(mathematicalRatio, 15); 
            
            let bgColor = funnelColors[idx];
            
            return `
            <div style="width: 100%;">
                <div class="funnel-bar" style="width: ${visualWidth}%; background: ${bgColor};">
                    <div class="funnel-title">${label}</div>
                    <div class="funnel-metrics">
                        <span class="funnel-val">${val}</span>
                        <span class="funnel-pct">(${pctHtml})</span>
                    </div>
                </div>
            </div>`;
        };

        let htmlFunnel = renderFunnelRow('Total Bruto (Pipefy)', f1, f1, f1, true, 0);
        htmlFunnel += renderFunnelRow('Base Elegível (Filtros: Serviço, Sem XML, Duplicadas)', f2, f1, f1, false, 1);
        htmlFunnel += renderFunnelRow('Sucesso no Lançamento (SAP)', f3, f2, f1, false, 2);
        htmlFunnel += renderFunnelRow('Contabilidade Validada (SIM)', f4, f3, f1, false, 3);
        htmlFunnel += renderFunnelRow('Parcela Ajustada (Automação)', f5, f4, f1, false, 4);
        
        document.getElementById('funnelContainer').innerHTML = htmlFunnel;

        // --- CÁLCULO DOS OUTROS GRÁFICOS ---
        let filtered = dateFiltered;

        // "Ocultar NFS": tira todo card de serviço (tipo de solicitação "Serviços" ou documento de serviço).
        if(hideNfs) {
            filtered = filtered.filter(r => r.Is_Servico !== true);
        }

        if(hideXml) {
            filtered = filtered.filter(r => r.Motivo_Raiz !== 'Falta de XML no Card');
        }
        
        if(hideDupes) {
            filtered = filtered.filter(r => r.Motivo_Raiz !== 'Nota Já Lançada / Duplicidade');
        }

        const selSituacao = document.getElementById('selSituacao').value;
        if(selSituacao === 'PENDENTES') {
            filtered = filtered.filter(ehPendente);
        }

        if(selValidado !== 'TODOS') {
            filtered = filtered.filter(r => {
                let val = String(r.Card_Validado || '').trim().toUpperCase();
                if (val === '' || val === 'NAN') {
                    val = 'NÃO';
                }
                return val === selValidado;
            });
        }

        let t = filtered.length, s = 0, e = 0;
        filtered.forEach(r => { if(r.Status_Final === 'SUCESSO') s++; else e++; });
        document.getElementById('kpiTotal').innerText = t;
        document.getElementById('kpiSuc').innerText = s;
        document.getElementById('kpiErr').innerText = e;
        document.getElementById('kpiStp').innerText = t > 0 ? (s/t*100).toFixed(1)+'%' : '0%';

        const groupStacked = (data, key) => {
            let map = {};
            data.forEach(r => {
                let val = r[key];
                let k = (!val || String(val).trim() === '' || val === 'nan') ? 'Não Informado' : String(val).trim();
                if(!map[k]) map[k] = { suc:0, err:0, tot:0 };
                map[k].tot++;
                if(r.Status_Final === 'SUCESSO') map[k].suc++; else map[k].err++;
            });
            return Object.entries(map).sort((a,b) => b[1].tot - a[1].tot); 
        };

        // --- EVOLUÇÃO DIÁRIA & LARGURA DINÂMICA (SCROLL) ---
        let trendMap = {};
        filtered.forEach(r => {
            if(!r.Date) return;
            if(!trendMap[r.Date]) trendMap[r.Date] = {s:0, e:0};
            if(r.Status_Final === 'SUCESSO') trendMap[r.Date].s++; else trendMap[r.Date].e++;
        });
        let trendKeys = Object.keys(trendMap).sort();
        
        let trendWrapper = document.getElementById('trendCanvasWrapper');
        let scrollContainer = document.getElementById('trendScrollContainer');
        
        // Mágica Matemática: Garante que exatos 30 dias ocupem 100% da tela visível do container.
        // Se houver 90 dias, a div interna terá 300% de largura (ativando o scroll perfeitamente).
        let dynamicWidth = Math.max(100, (trendKeys.length / 30) * 100);
        trendWrapper.style.width = dynamicWidth + '%';

        mk('cTrend', {
            type: 'bar',
            data: {
                labels: trendKeys,
                datasets: [
                    { label: 'Sucesso', data: trendKeys.map(k => trendMap[k].s), backgroundColor: '#3ecf6e', stack:'A', borderRadius:2 },
                    { label: 'Falha', data: trendKeys.map(k => trendMap[k].e), backgroundColor: '#f06060', stack:'A', borderRadius:2 }
                ]
            },
            options: { 
                responsive:true, maintainAspectRatio:false, 
                plugins:{ tooltip: stackedTooltip, legend: {display: false}, datalabels: {display:false} }, 
                scales:{ x:{grid:{display:false}, stacked:true}, y:{grid:GR, stacked:true} } 
            }
        });

        // Joga a barra de rolagem horizontal lá pro final (Hoje) depois que renderizar
        setTimeout(() => {
            if(scrollContainer) {
                scrollContainer.scrollLeft = scrollContainer.scrollWidth;
            }
        }, 300);

        let docData = groupStacked(filtered, 'Tipo_Doc');
        mk('cDocType', {
            type: 'bar',
            data: {
                labels: docData.map(d => d[0]),
                datasets: [
                    { label: 'Sucesso', data: docData.map(d => d[1].suc), backgroundColor: '#3ecf6e', stack:'A', borderRadius:2 },
                    { label: 'Falha', data: docData.map(d => d[1].err), backgroundColor: '#f06060', stack:'A', borderRadius:2 }
                ]
            },
            options: { 
                indexAxis:'y', responsive:true, maintainAspectRatio:false, 
                plugins:{ tooltip: stackedTooltip, legend: legendConfig, datalabels: { color: '#000', font: {weight: 'bold', size:10}, formatter: (val) => val > 0 ? val : '' } }, 
                scales:{ x:{grid:GR, stacked:true}, y:{grid:{display:false}, stacked:true} } 
            }
        });

        let reqData = groupStacked(filtered, 'Tipo_Sol');
        let topReqData = reqData.slice(0, 7); 
        
        if (reqData.length > 7) {
            let outrosSuc = 0, outrosErr = 0;
            for(let i=7; i<reqData.length; i++) {
                outrosSuc += reqData[i][1].suc;
                outrosErr += reqData[i][1].err;
            }
            topReqData.push(["Outros (Agrupado)", {suc: outrosSuc, err: outrosErr, tot: outrosSuc + outrosErr}]);
        }

        mk('cReqType', {
            type: 'bar',
            data: {
                labels: topReqData.map(d => d[0].length > 25 ? d[0].substring(0,25) + '...' : d[0]),
                datasets: [
                    { label: 'Sucesso', data: topReqData.map(d => d[1].suc), backgroundColor: '#3ecf6e', stack:'A', borderRadius:2 },
                    { label: 'Falha', data: topReqData.map(d => d[1].err), backgroundColor: '#f06060', stack:'A', borderRadius:2 }
                ]
            },
            options: { 
                indexAxis:'y', responsive:true, maintainAspectRatio:false, 
                plugins:{ tooltip: stackedTooltip, legend: legendConfig, datalabels: { color: '#000', font: {weight: 'bold', size:10}, formatter: (val) => val > 0 ? val : '' } }, 
                scales:{ x:{grid:GR, stacked:true}, y:{grid:{display:false}, stacked:true, ticks: {font: {size: 10}}} } 
            }
        });

        let errs = filtered.filter(r => r.Status_Final === 'FALHA');
        let errData = {};
        errs.forEach(r => { let a = r.Area_Responsavel || 'Não Informado'; errData[a] = (errData[a]||0) + 1; });
        let sortedErrArea = Object.entries(errData).sort((a,b)=>b[1]-a[1]);

        // se a área selecionada não existe mais no período/filtros atuais, limpa a seleção
        if(selectedArea && !sortedErrArea.some(d => d[0] === selectedArea)) {
            selectedArea = null;
        }

        mk('cArea', {
            type: 'bar',
            data: {
                labels: sortedErrArea.map(d => d[0]),
                datasets: [{
                    data: sortedErrArea.map(d => d[1]),
                    backgroundColor: sortedErrArea.map(d => d[0] === selectedArea ? '#f06060' : 'rgba(240,96,96,0.35)'),
                    borderColor: '#f06060',
                    borderWidth:1.5, borderRadius:3
                }]
            },
            options: { 
                indexAxis:'y', responsive:true, maintainAspectRatio:false, 
                onClick: (evt, elements) => {
                    if(elements.length > 0) {
                        let clicked = sortedErrArea[elements[0].index][0];
                        selectedArea = (selectedArea === clicked) ? null : clicked;
                        applyFilter();
                    }
                },
                onHover: (evt, elements) => {
                    evt.native.target.style.cursor = elements.length > 0 ? 'pointer' : 'default';
                },
                plugins:{ tooltip: singleTooltip, legend:{display:false}, datalabels: { color: '#dff0e4', anchor: 'end', align: 'end', font: {weight: 'bold', size:11} } }, 
                scales:{ x:{grid:GR, grace: '10%'}, y:{grid:{display:false}} } 
            }
        });

        // Pareto de motivos de erro — filtrado pela área selecionada no gráfico ao lado, se houver
        let paretoErrs = selectedArea ? errs.filter(r => (r.Area_Responsavel || 'Não Informado') === selectedArea) : errs;
        let rootMap = {};
        paretoErrs.forEach(r => { let m = r.Motivo_Raiz || 'Desconhecido'; rootMap[m] = (rootMap[m]||0) + 1; });
        let sortedRoots = Object.entries(rootMap).sort((a,b) => b[1] - a[1]);

        let areaBadge = document.getElementById('areaFilterBadge');
        if(selectedArea) {
            areaBadge.style.display = 'inline-block';
            areaBadge.textContent = `Área: ${selectedArea} ×`;
            areaBadge.onclick = () => { selectedArea = null; applyFilter(); };
        } else {
            areaBadge.style.display = 'none';
            areaBadge.textContent = '';
            areaBadge.onclick = null;
        }

        let pContainer = document.getElementById('paretoRoot');
        pContainer.innerHTML = '';
        if(sortedRoots.length === 0) {
            pContainer.innerHTML = selectedArea
                ? `<div style="color:var(--G); padding:20px; text-align:center;">Nenhuma falha da área "${selectedArea}" neste período!</div>`
                : '<div style="color:var(--G); padding:20px; text-align:center;">Nenhuma falha neste período! 🎉</div>';
        } else {
            let maxErr = sortedRoots[0][1];
            let totalErr = paretoErrs.length;
            sortedRoots.forEach(d => {
                let name = d[0], count = d[1];
                let widthPct = (count / maxErr * 100).toFixed(1);
                let repPct = (count / totalErr * 100).toFixed(1);
                
                pContainer.innerHTML += `
                <div class="prow">
                    <div class="plbl" title="${name}">${name}</div>
                    <div class="pbw"><div class="pbf" style="width:${widthPct}%"></div></div>
                    <div class="pv">${count}<br><span class="ppct">(${repPct}%)</span></div>
                </div>`;
            });
        }

        let tb = document.querySelector('#tTags tbody');
        tb.innerHTML = '';
        let tagMap = {};
        filtered.forEach(r => {
            let tags = r.Etiquetas ? String(r.Etiquetas).split(',') : ['Sem Etiqueta'];
            tags.forEach(t => {
                let tag = t.trim();
                if(!tag || tag === 'nan') return;
                if(!tagMap[tag]) tagMap[tag] = {suc:0, err:0, tot:0};
                tagMap[tag].tot++;
                if(r.Status_Final === 'SUCESSO') tagMap[tag].suc++; else tagMap[tag].err++;
            });
        });
        
        // --- TABELA DE PENDENTES (respeita período e filtros acima) ---
        let pend = filtered.filter(ehPendente).sort((a,b) => String(a.Date).localeCompare(String(b.Date)));
        let hojeMs = Date.now();
        document.getElementById('pendCount').textContent = `(${pend.length})`;
        let tbp = document.querySelector('#tPend tbody');
        tbp.innerHTML = pend.length === 0
            ? '<tr><td colspan="6" style="color:var(--G); text-align:center; padding:16px;">Nenhum card pendente com erro neste período! 🎉</td></tr>'
            : pend.map(r => {
                let dias = r.Date ? Math.max(0, Math.floor((hojeMs - new Date(r.Date + 'T00:00:00').getTime()) / 86400000)) : '';
                let link = r.Card_Id ? `<a href="https://app.pipefy.com/open-cards/${esc(r.Card_Id)}" target="_blank" style="color:var(--B); text-decoration:none;">${esc(r.Titulo || r.Card_Id)}</a>` : esc(r.Titulo);
                return `<tr>
                    <td class="tag-lbl" title="${esc(r.Titulo)}">${link}</td>
                    <td>${esc(r.Fase)}</td>
                    <td style="text-align:center; font-weight:bold; color:${dias >= 3 ? 'var(--R)' : 'var(--t)'}">${dias}</td>
                    <td>${esc(r.Area_Responsavel)}</td>
                    <td>${esc(r.Motivo_Raiz)}</td>
                    <td style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${esc(r.Erro)}">${esc(r.Erro)}</td>
                </tr>`;
            }).join('');

        if(histData && abaAtual === 'hist') renderHistorico();

        let sortedTags = Object.entries(tagMap).sort((a,b) => b[1].tot - a[1].tot).slice(0, 10);
        sortedTags.forEach(d => {
            let tag = d[0], v = d[1];
            let pctSuc = (v.suc / v.tot * 100).toFixed(1);
            let pctErr = (v.err / v.tot * 100).toFixed(1);
            
            tb.innerHTML += `
            <tr>
                <td class="tag-lbl" title="${tag}">${tag}</td>
                <td style="text-align:center">${v.tot}</td>
                <td style="color:var(--G); text-align:center; font-weight:bold;">${v.suc}</td>
                <td style="color:var(--R); text-align:center; font-weight:bold;">${v.err}</td>
                <td>
                    <div class="spk">
                        <div class="spks" style="width:${pctSuc}%" title="${pctSuc}% Sucesso"></div>
                        <div class="spkf" style="width:${pctErr}%" title="${pctErr}% Erro"></div>
                    </div>
                </td>
            </tr>`;
        });
    }

    // ---------------- HISTÓRICO DE ERROS (comentários dos cards) ----------------
    const histEmbutido = {{ hist_json | safe }};
    let histData = null, abaAtual = 'geral';
    function trocarAba(aba) {
        abaAtual = aba;
        document.getElementById('main').style.display = aba === 'geral' ? '' : 'none';
        document.getElementById('histMain').style.display = aba === 'hist' ? '' : 'none';
        document.getElementById('tabGeral').classList.toggle('on', aba === 'geral');
        document.getElementById('tabHist').classList.toggle('on', aba === 'hist');
        // filtros que não valem para o histórico ficam apagados
        ['selValidado', 'selSituacao'].forEach(id => {
            let el = document.getElementById(id); let alvo = el.closest('label') || el;
            alvo.style.opacity = aba === 'hist' ? '0.35' : '';
        });
        if(aba === 'hist') { if(histData) renderHistorico(); else carregarHistorico(); }
    }
    function carregarHistorico() {
        let st = document.getElementById('histStatus');
        if(histEmbutido.erro) { st.style.color = 'var(--R)'; st.textContent = 'Não foi possível montar o histórico: ' + histEmbutido.erro; return; }
        if(!histEmbutido.registros.length) { st.textContent = 'Nenhum erro registrado no campo "Histórico de Erros" (confira se a coluna está no relatório do Pipefy).'; return; }
        histData = histEmbutido; st.style.display = 'none';
        document.getElementById('histConteudo').style.display = '';
        renderHistorico();
    }
    function renderHistorico() {
        if(!histData) return;
        const start = document.getElementById('dtStart').value;
        const end = document.getElementById('dtEnd').value;
        const hideXml = document.getElementById('chkHideXml').checked;
        const hideDupes = document.getElementById('chkHideDupes').checked;
        const hideNfs = document.getElementById('chkHideNfs').checked;
        let regs = histData.registros.filter(r => (!start || !end || (r.Date >= start && r.Date <= end)));
        if(hideNfs) regs = regs.filter(r => r.Servico_Card !== true);
        if(hideXml) regs = regs.filter(r => r.Motivo_Raiz !== 'Falta de XML no Card');
        if(hideDupes) regs = regs.filter(r => r.Motivo_Raiz !== 'Nota Já Lançada / Duplicidade');
        document.getElementById('hGerado').textContent = `(atualizado em ${histData.gerado_em})`;

        let cards = {};
        regs.forEach(r => { cards[r.Card_Id] = r.Card_Lancado === 'SIM'; });
        let ids = Object.keys(cards);
        let depois = ids.filter(k => cards[k]).length;
        document.getElementById('hKpiOc').innerText = regs.length;
        document.getElementById('hKpiCards').innerText = ids.length;
        document.getElementById('hKpiDepois').innerText = depois;
        document.getElementById('hKpiSem').innerText = ids.length - depois;

        let area = {};
        regs.forEach(r => { let a = r.Area_Responsavel || 'Não Informado'; area[a] = (area[a]||0) + 1; });
        let areas = Object.entries(area).sort((a,b) => b[1]-a[1]);
        mk('hArea', { type:'bar',
            data: { labels: areas.map(d => d[0]), datasets: [{ data: areas.map(d => d[1]), backgroundColor:'rgba(240,96,96,0.35)', borderColor:'#f06060', borderWidth:1.5, borderRadius:3 }] },
            options: { indexAxis:'y', responsive:true, maintainAspectRatio:false,
                plugins:{ tooltip: singleTooltip, legend:{display:false}, datalabels:{ color:'#dff0e4', anchor:'end', align:'end', font:{weight:'bold', size:11} } },
                scales:{ x:{grid:GR, grace:'10%'}, y:{grid:{display:false}} } } });

        let mot = {};
        regs.forEach(r => { let m = r.Motivo_Raiz || 'Desconhecido'; mot[m] = (mot[m]||0) + 1; });
        let mots = Object.entries(mot).sort((a,b) => b[1]-a[1]);
        let hp = document.getElementById('hPareto');
        if(mots.length === 0) { hp.innerHTML = '<div style="color:var(--G); padding:20px; text-align:center;">Nenhum erro neste período! 🎉</div>'; }
        else {
            let mx = mots[0][1];
            hp.innerHTML = mots.map(d => `
                <div class="prow">
                    <div class="plbl" title="${esc(d[0])}">${esc(d[0])}</div>
                    <div class="pbw"><div class="pbf" style="width:${(d[1]/mx*100).toFixed(1)}%"></div></div>
                    <div class="pv">${d[1]}<br><span class="ppct">(${(d[1]/regs.length*100).toFixed(1)}%)</span></div>
                </div>`).join('');
        }

        let dia = {};
        regs.forEach(r => { if(r.Date) dia[r.Date] = (dia[r.Date]||0) + 1; });
        let dias = Object.keys(dia).sort();
        mk('hTrend', { type:'bar',
            data: { labels: dias, datasets: [{ label:'Ocorrências', data: dias.map(k => dia[k]), backgroundColor:'#f06060', borderRadius:2 }] },
            options: { responsive:true, maintainAspectRatio:false, plugins:{ legend:{display:false}, datalabels:{display:false} },
                scales:{ x:{grid:{display:false}}, y:{grid:GR} } } });

        let tb = document.querySelector('#tHist tbody');
        tb.innerHTML = regs.length === 0 ? '<tr><td colspan="7" style="color:var(--G); text-align:center; padding:16px;">Nenhum erro neste período! 🎉</td></tr>'
            : regs.slice(0, 1500).map(r => `<tr>
                <td>${esc(r.Hora)}</td>
                <td class="tag-lbl" title="${esc(r.Titulo)} (${esc(r.Fase)})"><a href="https://app.pipefy.com/open-cards/${esc(r.Card_Id)}" target="_blank" style="color:var(--B); text-decoration:none;">${esc(r.Titulo || r.Card_Id)}</a></td>
                <td>${esc(r.NF)}</td>
                <td>${esc(r.Area_Responsavel)}</td>
                <td>${esc(r.Motivo_Raiz)}</td>
                <td style="text-align:center; font-weight:bold; color:${r.Card_Lancado === 'SIM' ? 'var(--G)' : 'var(--R)'}">${r.Card_Lancado}</td>
                <td style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${esc(r.Erro)}">${esc(r.Erro)}</td>
            </tr>`).join('');
    }

    function baixarXlsx(e) {
        e.preventDefault();
        const start = document.getElementById('dtStart').value;
        const end = document.getElementById('dtEnd').value;
        const hideNfs = document.getElementById('chkHideNfs').checked;
        const hideXml = document.getElementById('chkHideXml').checked;
        const hideDupes = document.getElementById('chkHideDupes').checked;
        const cardValidado = document.getElementById('selValidado').value;
        const situacao = document.getElementById('selSituacao').value;
        
        let url = `export-excel?dtStart=${start}&dtEnd=${end}&hideNfs=${hideNfs}&hideXml=${hideXml}&hideDupes=${hideDupes}&cardValidado=${encodeURIComponent(cardValidado)}&situacao=${situacao}`;
        window.location.href = url;
    }

    window.addEventListener('DOMContentLoaded', () => {
        if (document.fonts && document.fonts.ready) {
            document.fonts.ready.then(() => {
                initFilter();
            });
        } else {
            initFilter();
        }
    });
    </script>
    </body>
    </html>
    """
    
    return render_template_string(html_template, records_json=records_json, hist_json=hist_json)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8010, debug=True)