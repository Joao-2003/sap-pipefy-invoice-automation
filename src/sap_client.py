import logging
import re
import requests
import copy
from requests.auth import HTTPBasicAuth
from datetime import datetime, timezone
import time
from config import (
    SAP_BASE_URL, SAP_API_PATH,
    SAP_AUTH_METHOD, SAP_USERNAME, SAP_PASSWORD,
    SAP_OAUTH_TOKEN_URL, SAP_OAUTH_CLIENT_ID, SAP_OAUTH_CLIENT_SECRET,
)

log = logging.getLogger(__name__)

# Base para Supplier Invoice
BASE = SAP_BASE_URL.rstrip("/") + SAP_API_PATH
# Bases Customizadas de Integração Fiscal e OData V2
BASE_NF = SAP_BASE_URL.rstrip("/") + "/sap/opu/odata/sap/API_LOGBR_NOTAFISCAL_SRV"
BASE_CDS_TAX = SAP_BASE_URL.rstrip("/") + "/sap/opu/odata/sap/YY1_NF_ITEM_TAX_CDS_CDS"
BASE_PAYM_DAYS = SAP_BASE_URL.rstrip("/") + "/sap/opu/odata/sap/YY1_PAYM_TERMS_DAYS_CDS"
BASE_PAYM_PCT = SAP_BASE_URL.rstrip("/") + "/sap/opu/odata/sap/YY1_PAYM_TERMS_PCT_CDS"
BASE_DEPARA = SAP_BASE_URL.rstrip("/") + "/sap/opu/odata/sap/YY1_FATURA_DEPARA_CDS"
BASE_SUPPLIER_ITEMS = SAP_BASE_URL.rstrip("/") + "/sap/opu/odata/sap/YY1_SUPPLIER_LINE_ITEMS_CDS"

# ─────────────────────────────────────────────────────────────────────────────
#  Mapeamentos de Negócio (Configurações do SAP)
# ─────────────────────────────────────────────────────────────────────────────

UNIT_MAP = {
    "PC": "UN", "PÇ": "UN", "UNID": "UN", "UND": "UN", "UN": "UN", "ST": "UN", 
    "UNI": "UN", "CJ": "UN", "CONJ": "UN", "JG": "UN",
    "KG": "KG", "GR": "G", "G": "G", "TON": "T", "T": "T",
    "MT": "M", "M": "M", "M2": "M2", "M3": "M3",
    "LT": "L", "L": "L", "CX": "CX", "DZ": "DZ",
    "PR": "PR", "SC": "SC", "PCT": "PAC", "PAR": "PR",
    "HR": "H", "H": "H", "MIN": "MIN", "KM": "KM",
    "CT": "CT", "CEN": "CT", "CENTO": "CT",
    "MH": "MIL", "MIL": "MIL",  # MH = milheiro (sigla usada pela um fornecedor em uCom da NF-e), alias pro bucket MIL ja existente
}

CONVERSAO_MATRIZ = {
    ("TON", "KG"): 1000.0, ("T", "KG"): 1000.0, ("KG", "TON"): 0.001,
    ("G", "KG"): 0.001, ("GR", "KG"): 0.001, ("KG", "G"): 1000.0,
    ("M3", "L"): 1000.0, ("L", "M3"): 0.001, ("DZ", "UN"): 12.0,
    ("UN", "DZ"): 1 / 12.0, ("CX", "UN"): 1.0, 
    ("CT", "UN"): 100.0,  
    ("UN", "CT"): 0.01,   
    ("KM", "M"): 1000.0, ("M", "KM"): 0.001, 
    ("UN", "M"): 1.0, ("M", "UN"): 1.0,  
    ("MIL", "UN"): 1000.0, ("UN", "MIL"): 0.001,
}

def _converter_unidade(unidade_nfe: str) -> str:
    unid_limpa = (unidade_nfe or "UN").upper().strip()
    if unid_limpa not in UNIT_MAP:
        raise ValueError(f"Unidade de medida '{unid_limpa}' desconhecida. Informe a TI para atualizar o dicionário UNIT_MAP no sistema.")
    return UNIT_MAP[unid_limpa]

def converter_quantidade(qtd_nf: float, unid_nf: str, unid_po: str) -> float:
    nf_u = unid_nf.upper().strip()
    po_u = unid_po.upper().strip()
    
    nf_u_norm = UNIT_MAP.get(nf_u, nf_u)
    po_u_norm = UNIT_MAP.get(po_u, po_u)

    if nf_u_norm == po_u_norm or not po_u: 
        return float(qtd_nf)
    
    chave = (nf_u_norm, po_u_norm)
    if chave in CONVERSAO_MATRIZ:
        return float(qtd_nf) * CONVERSAO_MATRIZ[chave]
    
    raise ValueError(f"O sistema não tem regras para converter a unidade da Nota ({nf_u}) para a unidade do Pedido ({po_u}).")

# ─────────────────────────────────────────────────────────────────────────────
#  Auth & CSRF
# ─────────────────────────────────────────────────────────────────────────────

_oauth_cache = {"token": None, "expires_at": 0}

def _get_oauth_token() -> str:
    now = time.time()
    if _oauth_cache["token"] and now < _oauth_cache["expires_at"] - 60: return _oauth_cache["token"]
    resp = requests.post(SAP_OAUTH_TOKEN_URL, data={"grant_type": "client_credentials"}, auth=HTTPBasicAuth(SAP_OAUTH_CLIENT_ID, SAP_OAUTH_CLIENT_SECRET), timeout=15)
    resp.raise_for_status()
    d = resp.json()
    _oauth_cache["token"], _oauth_cache["expires_at"] = d["access_token"], now + int(d.get("expires_in", 3600))
    return d["access_token"]

def _auth_headers() -> dict: 
    headers = {"Accept-Language": "pt-BR", "sap-language": "PT"}
    if SAP_AUTH_METHOD == "oauth2":
        headers["Authorization"] = f"Bearer {_get_oauth_token()}"
    return headers

def _auth_kwargs() -> dict: return {"auth": HTTPBasicAuth(SAP_USERNAME, SAP_PASSWORD)} if SAP_AUTH_METHOD == "basic" else {}

def _fetch_csrf_token(session: requests.Session) -> str:
    resp = session.get(f"{BASE}/", headers={**_auth_headers(), "x-csrf-token": "Fetch"}, **_auth_kwargs(), timeout=15)
    resp.raise_for_status()
    return resp.headers.get("x-csrf-token", "")

# ─────────────────────────────────────────────────────────────────────────────
#  Fase 1: Barreira Comercial (Notificação Compras)
# ─────────────────────────────────────────────────────────────────────────────

TOLERANCIA_DIAS_FASE1 = 10


def _valores_parcelas_batem(simulacao, duplicatas_xml, valor_total_xml, valor_concentrado_1a):
    """
    Confere os valores das parcelas do XML contra o rateio % da condição. `valor_concentrado_1a`
    (IPI e/ou frete) é tirado da base do rateio e somado inteiro na 1ª parcela -- prática comum
    de fornecedores. Com 0, é a conferência proporcional de sempre.
    Devolve a soma das diferenças se todas as parcelas batem, ou None.
    """
    tolerancia_valor = max(2.00, valor_total_xml * 0.0001)
    base = valor_total_xml - valor_concentrado_1a
    soma = 0.0
    for i, sim in enumerate(simulacao):
        projetado = base * (sim["pct"] / 100.0) + (valor_concentrado_1a if i == 0 else 0.0)
        diff = abs(projetado - float(duplicatas_xml[i]["vDup"]))
        # Tolerância proporcional ao valor da nota (0.01%, com piso de R$ 2.00): fornecedores que
        # arredondam o split em 2 casas decimais (ex.: 33,33/33,33/33,34) enquanto a condição no
        # SAP está configurada com 3 casas (ex.: P005 = 33,333/33,333/33,334) geram uma diferença
        # que escala com o porte da nota (caso real: NF 154225, diffs de R$ 3,97/R$ 3,97/R$ 7,94
        # numa nota de R$ 132.433,27).
        if diff > tolerancia_valor:
            return None
        soma += diff
    return soma


def _vence_ate_emissao(dup: dict, dt_emissao) -> bool:
    """True se a parcela do XML vence até a data de emissão (+1 dia de folga para fuso)."""
    from datetime import timedelta
    try:
        return datetime.strptime(str(dup.get("dVenc", ""))[:10], "%Y-%m-%d").date() <= dt_emissao + timedelta(days=1)
    except Exception:
        return False


def _validar_fase1_compras(session: requests.Session, condicoes_pedidos: list, duplicatas_xml: list, data_emissao_raw: str, valor_total_xml: float, valor_ipi_xml: float = 0.0, valor_frete_xml: float = 0.0, info: dict = None) -> str:
    """
    Simula e cruza a condição de pagamento dos pedidos contra o XML.
    Bloqueia o lançamento alertando o setor de Compras caso nenhuma condição validada bata com a nota.
    Quando mais de uma condição bate dentro da tolerância, fica com a que tiver a menor
    diferença acumulada (dias e depois valor) frente ao que o fornecedor realmente cobrou.

    Valores: aceita o rateio proporcional (como sempre) OU o IPI e/ou o frete da nota inteiros
    na 1ª parcela (decisão 30/09/2026). Datas: tolerância de TOLERANCIA_DIAS_FASE1 dias.

    Parcela já paga por adiantamento (decisão 30/09/2026, alinhado com analista fiscal/Contas a Pagar):
    se a NF tem UMA parcela a mais que a condição e essa 1ª parcela vence até a emissão, ela é
    tratada como a parcela antecipada e as demais são conferidas contra a condição aplicada sobre
    (total - 1ª parcela). A fatura continua sendo lançada pelo valor TOTAL (o Contas a Pagar dá
    baixa da parcela paga contra o adiantamento). Se uma condição bater do jeito normal, ela tem
    preferência. Quando `info` é passado, recebe {"parcela_antecipada": {...}} nesse caso.
    """
    condicoes_unicas = list(set(condicoes_pedidos))

    try:
        dt_emissao = datetime.strptime(data_emissao_raw[:10], "%Y-%m-%d").date()
    except Exception:
        raise ValueError("Divergência Comercial: A data de emissão do XML está inválida para realizar a conferência dos vencimentos projetados.")

    # Se o XML não tiver NENHUMA informação de vencimento (nem <dup>, nem infCpl), não existe base real
    # pra desconfiar do Pedido -- qualquer suposição seria só um palpite. Nesse caso, confiamos na condição
    # do Pedido diretamente, contanto que ela seja única (mesmo espírito da Fase 2, que também fica muda
    # quando não há duplicatas no XML). Só seguimos bloqueando se o próprio Pedido estiver ambíguo.
    if not duplicatas_xml:
        if len(condicoes_unicas) == 1:
            return condicoes_unicas[0]
        raise ValueError(
            f"Divergência Comercial: A Nota Fiscal do Fornecedor não declara vencimento (nem duplicatas, nem "
            f"informações complementares), e os Pedidos SAP vinculados têm condições de pagamento diferentes "
            f"entre si ({', '.join(condicoes_unicas)}). Não é possível decidir automaticamente qual confiar. "
            f"Corrija o Pedido de Compras."
        )

    melhor_cond = None
    melhor_score = None
    melhor_antecipada = None

    for cond in condicoes_unicas:
        # API 2: Puxa o fatiamento em porcentagens da Condição
        pct_url = f"{BASE_PAYM_PCT}/YY1_PAYM_TERMS_PCT?$filter=PaymentTerms eq '{cond}'&$format=json"
        r_pct = session.get(pct_url, headers={**_auth_headers(), "Accept": "application/json"}, **_auth_kwargs())
        parcelas = r_pct.json().get("d", {}).get("results", []) if r_pct.ok else []

        simulacao = []
        if not parcelas:
            simulacao.append({"term": cond, "pct": 100.0})
        else:
            for p in sorted(parcelas, key=lambda x: x.get("PaymentTermsInstallmentItem", "")):
                t = p.get("InstallmentItemPaymentTerms") or cond
                simulacao.append({"term": t, "pct": float(p.get("InstallmentItemPercent", 100.0))})

        # Se o número de parcelas projetado for diferente do cobrado pelo fornecedor, já descarta --
        # exceto o caso da parcela já paga por adiantamento (ver docstring).
        dups_avaliadas = duplicatas_xml
        total_avaliado = valor_total_xml
        parcela_antecipada = None
        if len(simulacao) != len(duplicatas_xml):
            if len(duplicatas_xml) == len(simulacao) + 1 and _vence_ate_emissao(duplicatas_xml[0], dt_emissao):
                parcela_antecipada = duplicatas_xml[0]
                dups_avaliadas = duplicatas_xml[1:]
                total_avaliado = valor_total_xml - float(duplicatas_xml[0].get("vDup", 0) or 0)
            else:
                continue

        # 1) Datas: cada parcela dentro da tolerância de dias
        match_all = True
        score_dias = 0
        for i, sim in enumerate(simulacao):
            dup_xml = dups_avaliadas[i]

            # API 1: Puxa a conversão da condição para dias líquidos
            days_url = f"{BASE_PAYM_DAYS}/YY1_PAYM_TERMS_DAYS?$filter=PaymentTerms eq '{sim['term']}'&$format=json"
            r_days = session.get(days_url, headers={**_auth_headers(), "Accept": "application/json"}, **_auth_kwargs())
            d_days = r_days.json().get("d", {}).get("results", []) if r_days.ok else []

            if d_days:
                reg = d_days[0]
                net_days = int(float(reg.get("NetPaymentDays", 0) or 0))
                if net_days == 0:
                    # Se o prazo líquido (ZTAGN) está zerado, o SAP usa a maior faixa de desconto
                    # (CashDiscount1Days/CashDiscount2Days) como vencimento efetivo (ex: V015 = "Venc. em 15 dias"
                    # gravado como 1ª faixa de desconto a 0%, não como prazo líquido).
                    net_days = int(max(
                        float(reg.get("CashDiscount1Days", 0) or 0),
                        float(reg.get("CashDiscount2Days", 0) or 0),
                    ))
            else:
                net_days = 0

            from datetime import timedelta
            dt_venc_proj = dt_emissao + timedelta(days=net_days)
            
            try:
                dt_venc_xml = datetime.strptime(dup_xml["dVenc"][:10], "%Y-%m-%d").date()
            except:
                match_all = False
                break

            diff_dias = abs((dt_venc_proj - dt_venc_xml).days)

            # Tolerância de dias exclusiva na Fase 1: pulos de feriado/fim de semana no faturamento,
            # "bônus de prazo" de fornecedores que antecipam o envio (ex.: 51 vs. 45 dias -- decisão
            # 22/09/2026) e erros de digitação de vencimento corrigidos no boleto (ex.: 30/35/60 no
            # XML para uma condição 30/45/60 -- decisão 30/09/2026, tolerância de 7 para 10 dias).
            if diff_dias > TOLERANCIA_DIAS_FASE1:
                match_all = False
                break

            score_dias += diff_dias

        if not match_all:
            continue

        # 2) Valores: proporcional; se não bater, IPI / frete / IPI+frete inteiros na 1ª parcela
        opcoes_concentracao = [("proporcional", 0.0)]
        if valor_ipi_xml > 0:
            opcoes_concentracao.append(("IPI na 1ª parcela", valor_ipi_xml))
        if valor_frete_xml > 0:
            opcoes_concentracao.append(("frete na 1ª parcela", valor_frete_xml))
        if valor_ipi_xml > 0 and valor_frete_xml > 0:
            opcoes_concentracao.append(("IPI + frete na 1ª parcela", valor_ipi_xml + valor_frete_xml))

        score_valor = None
        for rotulo, concentrado in opcoes_concentracao:
            score_valor = _valores_parcelas_batem(simulacao, dups_avaliadas, total_avaliado, concentrado)
            if score_valor is not None:
                if concentrado:
                    log.info(f"Fase 1: condição {cond} aceita com {rotulo} (R$ {concentrado:.2f}).")
                break
        match_all = score_valor is not None

        if match_all:
            # Se mais de uma condição dos pedidos envolvidos bater dentro da tolerância, fica
            # com a que tiver a menor diferença acumulada (primeiro dias, depois valor) frente
            # ao que o fornecedor realmente cobrou no XML — em vez de ficar com a primeira
            # encontrada na iteração (que não tinha nenhum significado de negócio).
            score = (1 if parcela_antecipada else 0, score_dias, score_valor)
            if melhor_cond is None or score < melhor_score:
                melhor_cond = cond
                melhor_score = score
                melhor_antecipada = parcela_antecipada

    if melhor_cond is not None:
        if melhor_antecipada is not None:
            log.warning(
                f"Fase 1: condição {melhor_cond} aceita considerando a 1ª parcela da NF "
                f"(R$ {float(melhor_antecipada.get('vDup', 0) or 0):.2f}, venc. {melhor_antecipada.get('dVenc', '')[:10]}) "
                f"como já paga por adiantamento. Fatura lançada pelo valor total."
            )
            if info is not None:
                info["parcela_antecipada"] = dict(melhor_antecipada)
        return melhor_cond  # Bateu! Retorna o código que deve ser gravado na MIRO.

    # Se varreu TODAS as condições dos pedidos envolvidos e NENHUMA bateu com o XML:
    raise ValueError(f"Divergência Comercial: As Condições de Pagamento amarradas aos Pedidos SAP ({', '.join(condicoes_unicas)}) não conferem com as datas ou valores cobrados na Nota Fiscal do Fornecedor. Corrija o Pedido de Compras.")

# ─────────────────────────────────────────────────────────────────────────────
#  Fase 2: Auditoria Financeira Pós-Lançamento (Notificação Contas a Pagar)
# ─────────────────────────────────────────────────────────────────────────────

def _auditar_fase2_financeiro(session: requests.Session, doc_num: str, doc_year: str, company_code: str, dados_nf: dict) -> str:
    """
    Busca as Partidas em Aberto (Dívida) recém gerada no SAP e audita se o Vencimento e os Centavos 
    (individualmente por parcela) estão fiéis à cobrança do Fornecedor no XML.
    """
    awkey = f"{doc_num}{doc_year}"
    
    url_depara = f"{BASE_DEPARA}/YY1_FATURA_DEPARA?$filter=OriginalReferenceDocument eq '{awkey}' and CompanyCode eq '{company_code}'&$format=json"
    
    # Aguarda o milissegundo de atraso do motor de Journal Entry do SAP
    res_dep = []
    for _ in range(3):
        r_dep = session.get(url_depara, headers={**_auth_headers(), "Accept": "application/json"}, **_auth_kwargs())
        res_dep = r_dep.json().get("d", {}).get("results", []) if r_dep.ok else []
        if res_dep: break
        time.sleep(2)
        
    if not res_dep:
        return "" 
        
    acc_doc = res_dep[0].get("AccountingDocument")
    
    url_lines = f"{BASE_SUPPLIER_ITEMS}/YY1_SUPPLIER_LINE_ITEMS?$filter=AccountingDocument eq '{acc_doc}' and CompanyCode eq '{company_code}' and FiscalYear eq '{doc_year}'&$format=json"
    r_lines = session.get(url_lines, headers={**_auth_headers(), "Accept": "application/json"}, **_auth_kwargs())
    res_lines = r_lines.json().get("d", {}).get("results", []) if r_lines.ok else []
    
    # Isola o passivo real (exclui pernas de estoque e impostos da visão contábil)
    # Ignora partidas já compensadas (ex.: parcelas antigas depois do reparcelamento da Fase 3) e de
    # razão especial; no momento do lançamento nenhuma parcela está compensada, então nada muda ali.
    # Só é parcela a linha com VENCIMENTO: a CDS também traz o fornecedor em linhas que não são contas a pagar
    # (ex.: NF 02152, lançamento 5115012690: linha +75,00 sem vencimento contada como 2ª parcela).
    supplier_lines = [line for line in res_lines if line.get("Supplier", "").strip() != ""
                      and line.get("NetDueDate")
                      and not str(line.get("ClearingJournalEntry") or "").strip()
                      and not str(line.get("SpecialGLCode") or "").strip()]
    
    if not supplier_lines:
        return ""

    alertas = []
    hoje = datetime.now(timezone.utc).date()
    duplicatas_xml = dados_nf.get("duplicatas", [])
    # Impostos retidos: o esperado é o XML com a retenção somada na última parcela (procedimento do
    # Contas a Pagar, ver ajuste_parcelas.duplicatas_com_retencao). Sem retenção, nada muda.
    retencao_somada = 0.0
    try:
        import ajuste_parcelas as _ap
        _total_sap = sum(abs(float(l.get("AmountInCompanyCodeCurrency", 0))) for l in supplier_lines)
        duplicatas_xml, _ret = _ap.duplicatas_com_retencao(dados_nf, f"{_total_sap:.2f}")
        retencao_somada = float(_ret)
    except Exception as e:
        log.info("[Fase2] não consegui considerar a retenção: %s", e)
    
    # 2.1 - Auditoria Estrutural: A quantidade de parcelas geradas no SAP bate com o XML?
    if duplicatas_xml and len(supplier_lines) != len(duplicatas_xml):
        alertas.append(f"Divergência de Parcelamento: O SAP gerou {len(supplier_lines)} parcela(s) no passivo, mas o XML possui {len(duplicatas_xml)} duplicata(s).")
        
    # 2.2 - Auditoria Parcela a Parcela (Valores Exatos e Vencimentos Reais)
    elif duplicatas_xml and len(supplier_lines) == len(duplicatas_xml):
        supplier_lines.sort(key=lambda x: str(x.get("AccountingDocumentItem", "")))
        
        for idx, line in enumerate(supplier_lines):
            dup_xml = duplicatas_xml[idx]
            
            # --- Validação Implacável de Centavos da Parcela ---
            valor_sap = abs(float(line.get("AmountInCompanyCodeCurrency", 0)))
            valor_dup_xml = float(dup_xml.get("vDup", 0))
            
            if abs(valor_sap - valor_dup_xml) >= 0.01:
                alertas.append(f"Divergência de Valor na Parcela {idx+1}: O SAP gravou R$ {valor_sap:.2f}, mas o XML cobra R$ {valor_dup_xml:.2f}.")

            # --- Validação Estratégica de Vencimentos da Parcela ---
            net_due_ms = line.get("NetDueDate")
            if not net_due_ms: continue
            
            try:
                ms = int(net_due_ms.replace("/Date(", "").replace(")/", ""))
                dt_venc_sap = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()
            except:
                continue
                
            try:
                dt_venc_xml = datetime.strptime(dup_xml["dVenc"], "%Y-%m-%d").date()
            except:
                continue
            
            # Tolerância ZERO para encurtamento do prazo
            if dt_venc_sap < dt_venc_xml:
                alertas.append(f"Vencimento Encurtado: A parcela {idx+1} foi gravada para {dt_venc_sap.strftime('%d/%m/%Y')} no SAP, mas o XML exige até {dt_venc_xml.strftime('%d/%m/%Y')}.")
            
            # Tolerância ZERO para esticar o prazo indevidamente (Diferença absoluta > 0)
            elif abs((dt_venc_sap - dt_venc_xml).days) >= 1:
                alertas.append(f"Divergência de Data: A parcela {idx+1} foi gravada para {dt_venc_sap.strftime('%d/%m/%Y')} no SAP, mas o XML diz {dt_venc_xml.strftime('%d/%m/%Y')}.")
                
            # Alerta Instantâneo de Mora
            if dt_venc_sap < hoje:
                alertas.append(f"Vencimento Precoce (Mora): A parcela {idx+1} foi gravada com data retroativa ({dt_venc_sap.strftime('%d/%m/%Y')}), entrando em atraso instantâneo no SAP.")

    if alertas:
        # Retenções na NF (retTrib): o fornecedor cobra o líquido (vLiq) e o SAP gravou as parcelas
        # sobre o total da NF. Explica a diferença para o Contas a Pagar já saber o que fazer.
        explicacao = ""
        ret = dados_nf.get("retencoes") or {}
        if retencao_somada > 0:
            nomes = {"vRetPIS": "PIS", "vRetCOFINS": "COFINS", "vRetCSLL": "CSLL", "vIRRF": "IRRF", "vRetPrev": "INSS",
                     "vISSRet": "ISS"}
            detalhe = " + ".join(f"{nomes.get(k, k)} R$ {float(v):.2f}" for k, v in ret.items())
            explicacao = (f"\nℹ️ NF com impostos RETIDOS ({detalhe} = R$ {retencao_somada:.2f}): o valor esperado da "
                          f"última parcela é a duplicata do XML + R$ {retencao_somada:.2f} (total igual ao da NF).")
        return (
            "⚠️ *Alerta de Validação (Contas a Pagar):*\n"
            f"📄 Lançamento Contábil: {acc_doc} / Exercício {doc_year}\n\n"
            + "\n".join([f"• {a}" for a in set(alertas)])
            + explicacao
            + "\n_Ação do Financeiro estritamente necessária no SAP._"
        )
    
    return ""

# ─────────────────────────────────────────────────────────────────────────────
#  Fase 3: ajuste automático de VENCIMENTO (só data) das parcelas do fornecedor
# ─────────────────────────────────────────────────────────────────────────────
import os as _os
# Liga/desliga sem deploy (no .env, depois reiniciar o serviço; 0 = volta a só alertar):
#   FASE3_AJUSTAR_DATAS=0  -> não corrige vencimento
#   FASE3_REPARCELAR=0     -> não corrige valor das parcelas
FASE3_AJUSTAR_DATAS = _os.getenv("FASE3_AJUSTAR_DATAS", "1").strip() == "1"
FASE3_REPARCELAR = _os.getenv("FASE3_REPARCELAR", "1").strip() == "1"
# 1ª parcela já paga por adiantamento (XML com uma duplicata a mais, vencendo até a emissão):
# reparcela igual ao XML, 1ª parcela bloqueada (A) para compensar com o adiantamento.
# Conferido contra o ajuste manual da analista do Contas a Pagar na NF 70 (doc. 1505432702): mesmas parcelas, valores e
# datas. A compensação com o adiantamento continua manual. Desligar com FASE3_PARCELA_ANTECIPADA=0.
FASE3_PARCELA_ANTECIPADA = _os.getenv("FASE3_PARCELA_ANTECIPADA", "1").strip() == "1"
# Depois do reparcelamento, compensa a parcela antecipada (bloqueada A) com o adiantamento A em aberto.
# Testado em produção na NF 70 (05/10: 1505432702/001 x adiantamento 1592551694/002, compensação 1507531013).
# Desligar com FASE3_COMPENSAR_ADIANTAMENTO=0. Se falhar, a parcela continua bloqueada e fica o aviso.
FASE3_COMPENSAR_ADIANTAMENTO = _os.getenv("FASE3_COMPENSAR_ADIANTAMENTO", "1").strip() == "1"
# Vencimento do XML no passado: a parcela fica IGUAL À NF mesmo que esteja em dia no SAP e o ajuste a deixe
# vencida (Contas a Pagar, analista do Contas a Pagar: NF 3151 em 09/10 e NF 44383 em 14/10, "ajuste conforme a NF").
# FASE3_VENCIMENTO_IGUAL_NF=0 volta à regra anterior (não ajusta parcela em dia no SAP que ficaria vencida).
FASE3_VENCIMENTO_IGUAL_NF = _os.getenv("FASE3_VENCIMENTO_IGUAL_NF", "1").strip() == "1"
# Adiantamento pago antes do lançamento para QUALQUER parcela (NF 44383: KZ 1537556043 "id 1463331410, 2º PARCELA"):
# com as parcelas iguais à NF, compensa cada parcela com o adiantamento A em aberto do fornecedor que tem o ID do
# card e o mesmo valor. Desligar com FASE3_ADIANTAMENTO_CARD=0.
FASE3_ADIANTAMENTO_CARD = _os.getenv("FASE3_ADIANTAMENTO_CARD", "1").strip() == "1"
# Ajuste das parcelas (Fase 3 + adiantamentos do card) ADIADO para a entrada do card na fase Pagamento
# (fase_pagamento.py), depois da validação contábil -- pedido do João, 14/10/2026: ajustar logo após o lançamento
# obrigava a desfazer o ajuste toda vez que o lançamento precisava ser estornado (NF 555333, NF 44902).
# Liga quando PAGAMENTO_PHASE_ID está no .env; vazio = ajusta logo após o lançamento (como antes).
PAGAMENTO_PHASE_ID = _os.getenv("PAGAMENTO_PHASE_ID", "").strip()
FASE3_NO_PAGAMENTO = bool(PAGAMENTO_PHASE_ID)
AVISO_AJUSTE_NO_PAGAMENTO = ("ℹ️ A automação ajusta as parcelas quando o card entrar na fase Pagamento (depois da "
                             "validação contábil). Não é preciso ajustar agora.")


def _sap_ja_vencida(data_sap, hoje) -> bool:
    """A parcela do SAP já está vencida (data conhecida e anterior a hoje)."""
    return data_sap is not None and data_sap < hoje


def _fase3_ajustar_parcelas(doc_num: str, doc_year: str, company_code: str, dados_nf: dict, card_data: dict) -> dict:
    """
    Corrige as parcelas do fornecedor para ficarem iguais às duplicatas do XML (lógica e mensagens SOAP
    em ajuste_parcelas.py). Dois casos, sempre com a MESMA quantidade de parcelas e o MESMO total:
      - só o vencimento difere: altera o vencimento no próprio item (Change; sem documento novo).
        Testado em produção: NF 54058 (CLI) e NF 315251 (automação).
      - o valor das parcelas difere: RE com as parcelas novas + contrapartida e compensação das antigas.
        Testado em produção: NF 15034 (CLI, 02/10/2026).
    Não age se: alguma parcela já compensada, quantidade de parcelas ou total diferente (exceto parcela
    antecipada por adiantamento), ou -- só com FASE3_VENCIMENTO_IGUAL_NF=0 -- vencimento do XML no passado
    para parcela que está em dia no SAP.
    Retorna {} se não se aplica; senão {"tipo": "data"|"valor", ...}.
    """
    import uuid
    import ajuste_parcelas as ap

    leitor = ap.LeitorSAP()
    acc_doc = leitor.documento_contabil(doc_num, doc_year, company_code)
    if not acc_doc:
        return {}
    linhas = leitor.linhas(acc_doc, doc_year, company_code)
    try:
        fatura = leitor.fatura(doc_num, doc_year)
    except Exception as e:
        log.info("[Fase3] não consegui ler a fatura %s/%s: %s", doc_num, doc_year, e)
        fatura = {}
    plano = ap.montar_plano(fatura, linhas, dados_nf, card_data.get("card_id"))
    acao = plano.get("acao")
    hoje = datetime.now(timezone.utc).date()

    def out(msg):
        msg = str(msg).strip()
        if msg:
            log.info("[Fase3] %s", msg)
            out.ultima = msg
    out.ultima = ""

    s = requests.Session()
    s.auth = HTTPBasicAuth(SAP_USERNAME, SAP_PASSWORD)
    pos = {str(l.get("AccountingDocumentItem")).zfill(3): i + 1 for i, l in enumerate(plano.get("atuais", []))}

    if acao == "alterar_data" and plano.get("datas") and FASE3_AJUSTAR_DATAS:
        # Vencimento do XML no passado: só ajusta se a parcela do SAP também já estiver vencida (o atraso já
        # existe; a data fica igual à da NF -- pedido da Contas a Pagar, 09/10, NF 3151: SAP 28/09 x XML 25/09).
        # Barra só quando a parcela está em dia no SAP e o ajuste a deixaria vencida.
        if not FASE3_VENCIMENTO_IGUAL_NF and any(
                alvo < hoje and not _sap_ja_vencida(ap._data_odata(l.get("NetDueDate")), hoje)
                for l, alvo in plano["datas"]):
            log.info("[Fase3] NF %s: vencimento do XML já passou e a parcela do SAP está em dia; não ajusto "
                     "(fica o alerta).", dados_nf.get("numero_nf"))
            return {}
        ep = ap._endpoint(ap.WSDL_CHANGE, SAP_BASE_URL.rstrip("/") + "/sap/bc/srt/scs_ext/sap/journalentrybulkchangerequest_")
        log.info("[Fase3] NF %s / lançamento %s: ajustando vencimento de %d parcela(s).",
                 dados_nf.get("numero_nf"), acc_doc, len(plano["datas"]))
        try:
            ok = ap.executar_alteracao_datas(s, leitor, plano, company_code, doc_year, acc_doc, ep,
                                             uuid.uuid4().hex[:8].upper(), out=out)
        except Exception as e:
            ok = False
            out(f"erro: {e}")
        ajustados = [(pos.get(item, "?"), item, de, para) for item, de, para in plano.get("ajustados", [])]
        return {"tipo": "data", "acc_doc": acc_doc, "ajustados": ajustados,
                "falha": None if ok else (out.ultima or "não concluído")}

    if acao == "ajustar" and FASE3_REPARCELAR:
        novas = plano.get("novas", [])
        n_atuais = len(plano.get("atuais", []))
        # NF 44383: adiantamento dentro das parcelas (mesma quantidade, 1ª duplicata vence na emissão). Só vira
        # antecipada (bloqueio A + compensação) se houver adiantamento A em aberto com o mesmo valor.
        if plano.get("candidata_antecipada") and FASE3_PARCELA_ANTECIPADA and fatura.get("InvoicingParty"):
            try:
                if ap.marcar_antecipada_mesma_qtd(plano, leitor.adiantamentos_abertos(fatura["InvoicingParty"],
                                                                                      company_code)):
                    log.info("[Fase3] NF %s: 1ª duplicata vence na emissão e há adiantamento A com o mesmo valor "
                             "(mesma quantidade de parcelas).", dados_nf.get("numero_nf"))
            except Exception as e:
                log.info("[Fase3] não consegui ler os adiantamentos do fornecedor: %s", e)
        antecipada = bool(novas) and novas[0].get("antecipada") and len(novas) in (n_atuais, n_atuais + 1)
        if antecipada and not FASE3_PARCELA_ANTECIPADA:
            log.info("[Fase3] NF %s: 1ª parcela paga por adiantamento; ajuste automático desligado "
                     "(FASE3_PARCELA_ANTECIPADA=0) -- fica o alerta.", dados_nf.get("numero_nf"))
            return {}
        if not antecipada and (len(novas) != len(plano.get("atuais", [])) or any(p.get("antecipada") for p in novas)):
            log.info("[Fase3] NF %s: quantidade de parcelas diferente do XML; fica o alerta (ajuste manual).",
                     dados_nf.get("numero_nf"))
            return {}
        # a parcela antecipada vence no passado por natureza (já foi paga): não entra nesta checagem.
        # Vencimento do XML no passado só é aceito se a parcela correspondente do SAP também já estiver vencida
        # (o atraso já existe; data e valor ficam iguais à NF -- NF 79892 e NF 3151). Parcela em dia no SAP que
        # ficaria vencida com o ajuste: fica o alerta.
        datas_sap = [ap._data_odata(l.get("NetDueDate")) for l in plano.get("atuais", [])]
        desloc = 1 if antecipada and len(novas) == n_atuais + 1 else 0
        for i, p in enumerate(novas if not FASE3_VENCIMENTO_IGUAL_NF else []):
            if p.get("antecipada") or p["data_base"] >= hoje:
                continue
            j = i - desloc
            if not (0 <= j < len(datas_sap) and _sap_ja_vencida(datas_sap[j], hoje)):
                log.info("[Fase3] NF %s: vencimento do XML já passou e a parcela do SAP está em dia; não reparcelo "
                         "(fica o alerta).", dados_nf.get("numero_nf"))
                return {}
        if not fatura.get("InvoicingParty"):
            return {}
        if antecipada:
            try:
                ap.vincular_adiantamento(plano, leitor.adiantamentos_abertos(fatura["InvoicingParty"], company_code))
            except Exception as e:
                log.info("[Fase3] não consegui ler os adiantamentos do fornecedor: %s", e)
        log.info("[Fase3] NF %s / lançamento %s: reparcelando %d parcela(s) (valores diferentes do XML%s).",
                 dados_nf.get("numero_nf"), acc_doc, len(novas), "; 1ª já paga por adiantamento" if antecipada else "")
        try:
            res = ap.executar_reparcelamento(s, leitor, plano, fatura, dados_nf, company_code, doc_year, acc_doc, out=out)
        except Exception as e:
            res = {"falha": f"erro: {e}", "doc_novo": None}
        comp_adto = None
        if antecipada and res.get("ok") and plano.get("adiantamento") and FASE3_COMPENSAR_ADIANTAMENTO:
            try:
                p1 = novas[0]
                cands = [l for l in leitor.linhas(res["doc_novo"], res.get("ano_novo") or doc_year, company_code)
                         if str(l.get("Supplier") or "").strip()
                         and not str(l.get("ClearingJournalEntry") or "").strip()
                         and str(l.get("PaymentBlockingReason") or "").strip() == str(p1.get("bloqueio") or "").strip()
                         and ap._data_odata(l.get("NetDueDate")) == p1["data_base"]
                         and abs(-ap._dec(l["AmountInCompanyCodeCurrency"]) - ap._dec(p1["valor"])) <= ap.TOLERANCIA_IGUAL]
                if len(cands) == 1:
                    comp_adto = ap.compensar_parcela_com_adiantamento(
                        s, leitor, cands[0], plano["adiantamento"], fatura["InvoicingParty"], company_code,
                        str(dados_nf.get("numero_nf") or ""), out=out)
                else:
                    comp_adto = {"ok": False, "falha": f"parcela 1 não localizada no doc. {res['doc_novo']} "
                                                       f"({len(cands)} candidata(s))"}
            except Exception as e:
                comp_adto = {"ok": False, "falha": f"erro: {e}"}
            log.info("[Fase3] NF %s: compensação parcela 1 x adiantamento: %s", dados_nf.get("numero_nf"),
                     comp_adto.get("doc_comp") if comp_adto.get("ok") else comp_adto.get("falha"))
        mudancas = [(i + 1, -ap._dec(l["AmountInCompanyCodeCurrency"]), p["valor"], p["data_base"])
                    for i, (l, p) in enumerate(zip(plano["atuais"], novas))]
        adto = plano.get("adiantamento")
        return {"tipo": "valor", "acc_doc": acc_doc, "mudancas": mudancas, "doc_novo": res.get("doc_novo"),
                "tipo_doc": ap.TIPO_DOC_NOVAS,
                "antecipada": antecipada, "novas": [(i + 1, p["valor"], p["data_base"], p.get("bloqueio", ""))
                                                    for i, p in enumerate(novas)],
                "adiantamento": (f"{adto['AccountingDocument']}/{adto['FiscalYear']} item {adto['AccountingDocumentItem']}"
                                 if adto else None),
                "adiantamento_chave": ap._chave_item(adto) if adto else None,
                "ano_novo": res.get("ano_novo") or doc_year,
                "doc_comp": res.get("doc_comp"), "problemas": res.get("problemas", []),
                "comp_adto": comp_adto, "retencao": float(plano.get("retencao") or 0),
                "falha": res.get("falha") if not res.get("ok") else None}

    return {}


def _compensar_adiantamentos_card(doc_num: str, doc_year: str, company_code: str, dados_nf: dict, card_data: dict,
                                  res_f3: dict = None) -> list:
    """Compensa parcelas em aberto com adiantamentos A do fornecedor que têm o ID do card (qualquer parcela).
    Usa o documento das parcelas novas quando a Fase 3 reparcelou; senão o lançamento da fatura."""
    import uuid
    import ajuste_parcelas as ap
    card_id = str(card_data.get("card_id") or "").strip()
    if not (FASE3_ADIANTAMENTO_CARD and card_id):
        return []
    res_f3 = res_f3 or {}
    leitor = ap.LeitorSAP()
    fornecedor = str(leitor.fatura(doc_num, doc_year).get("InvoicingParty") or "").strip()
    if not fornecedor:
        return []
    adtos = leitor.adiantamentos_abertos(fornecedor, company_code)
    if not any(ap.tem_card(c, card_id) for c in adtos):
        return []
    if res_f3.get("tipo") == "valor" and res_f3.get("doc_novo"):
        doc, ano = res_f3["doc_novo"], str(res_f3.get("ano_novo") or doc_year)
    else:
        doc, ano = leitor.documento_contabil(doc_num, doc_year, company_code), doc_year
    if not doc:
        return []
    excluir = {res_f3["adiantamento_chave"]} if res_f3.get("adiantamento_chave") and (res_f3.get("comp_adto") or {}).get("ok") else set()
    pares = ap.casar_adiantamentos_do_card(leitor.linhas(doc, ano, company_code), adtos, card_id, excluir)
    s = requests.Session()
    s.auth = HTTPBasicAuth(SAP_USERNAME, SAP_PASSWORD)
    resultado = []
    for parc, adto, n in pares:
        log.info("[Adto] NF %s: parcela %s (%s/%s item %s, R$ %s) x adiantamento %s/%s item %s do card %s",
                 dados_nf.get("numero_nf"), n, parc["AccountingDocument"], parc.get("FiscalYear"),
                 parc["AccountingDocumentItem"], -ap._dec(parc["AmountInCompanyCodeCurrency"]),
                 adto["AccountingDocument"], adto.get("FiscalYear"), adto["AccountingDocumentItem"], card_id)
        try:
            r = ap.compensar_parcela_com_adiantamento(s, leitor, parc, adto, fornecedor, company_code,
                                                      str(dados_nf.get("numero_nf") or ""),
                                                      message_id=f"AJADTO_{uuid.uuid4().hex[:10].upper()}",
                                                      out=lambda m: log.info("[Adto] %s", str(m).strip()))
        except Exception as e:
            r = {"ok": False, "falha": f"erro: {e}"}
        resultado.append({"n": n, "valor": -ap._dec(parc["AmountInCompanyCodeCurrency"]),
                          "venc": ap._data_odata(parc.get("NetDueDate")),
                          "parcela": f"{parc['AccountingDocument']}/{parc.get('FiscalYear')} item {parc['AccountingDocumentItem']}",
                          "adto": f"{adto['AccountingDocument']}/{adto.get('FiscalYear')} item {adto['AccountingDocumentItem']}",
                          "texto_adto": str(adto.get("DocumentItemText") or "")[:50],
                          "ok": bool(r.get("ok")), "doc_comp": r.get("doc_comp"), "falha": r.get("falha")})
    return resultado


def _so_alerta_de_mora(alerta: str) -> bool:
    """O alerta da auditoria só tem itens de "Vencimento Precoce (Mora)"."""
    itens = [l.strip()[1:].strip() for l in str(alerta or "").splitlines() if l.strip().startswith("•")]
    return bool(itens) and all(i.startswith("Vencimento Precoce (Mora)") for i in itens)


def _parcelas_pagas_iguais_ao_xml(doc_num, doc_year, company_code, dados_nf) -> list:
    """Números das parcelas já compensadas quando TODAS as parcelas do lançamento da fatura (compensadas ou não)
    são iguais às duplicatas do XML (quantidade, valor e vencimento). Lista vazia se não for esse o caso.
    Parcela reparcelada pela Fase 3 não entra aqui: as antigas compensadas diferem do XML."""
    import ajuste_parcelas as ap
    try:
        leitor = ap.LeitorSAP()
        acc = leitor.documento_contabil(doc_num, doc_year, company_code)
        if not acc:
            return []
        linhas = sorted([l for l in leitor.linhas(acc, doc_year, company_code)
                         if str(l.get("Supplier") or "").strip() and l.get("NetDueDate")
                         and not str(l.get("SpecialGLCode") or "").strip()],
                        key=lambda l: str(l.get("AccountingDocumentItem", "")))
        total = sum(abs(ap._dec(l.get("AmountInCompanyCodeCurrency"))) for l in linhas)
        dups, _ret = ap.duplicatas_com_retencao(dados_nf, f"{total:.2f}")
        if not linhas or len(linhas) != len(dups):
            return []
        pagas = []
        for i, (l, d) in enumerate(zip(linhas, dups), start=1):
            if abs(abs(ap._dec(l.get("AmountInCompanyCodeCurrency"))) - ap._dec(d.get("vDup"))) >= ap.Decimal("0.01"):
                return []
            if str(ap._data_odata(l.get("NetDueDate"))) != str(d.get("dVenc") or "")[:10]:
                return []
            if str(l.get("ClearingJournalEntry") or "").strip():
                pagas.append(i)
        return pagas
    except Exception as e:
        log.info("[Pagamento] não consegui conferir parcelas pagas x XML: %s", e)
        return []


def _parcelas_iguais_ao_xml(doc_num, doc_year, company_code, dados_nf, card_data) -> bool:
    """As parcelas do lançamento já são iguais às duplicatas do XML (mesmo plano da Fase 3, só leitura)."""
    import ajuste_parcelas as ap
    try:
        leitor = ap.LeitorSAP()
        acc = leitor.documento_contabil(doc_num, doc_year, company_code)
        if not acc:
            return False
        plano = ap.montar_plano(leitor.fatura(doc_num, doc_year), leitor.linhas(acc, doc_year, company_code),
                                dados_nf, card_data.get("card_id"))
        return plano.get("acao") == "nada" and str(plano.get("motivo", "")).startswith("As parcelas do SAP já estão iguais")
    except Exception as e:
        log.info("[Pagamento] não consegui conferir as parcelas x XML: %s", e)
        return False


def ajustar_parcelas_da_fatura(doc_num: str, doc_year: str, company_code: str, dados_nf: dict, card_data: dict) -> dict:
    """Auditoria (Fase 2) + ajuste (Fase 3) + adiantamentos do card de uma fatura JÁ lançada -- chamado na entrada
    do card na fase Pagamento (fase_pagamento.py). Devolve {"ok", "ajustou", "texto"}:
      ok=True  -> parcelas iguais à NF (já estavam, ou o ajuste deu certo) e adiantamentos do card compensados;
      ok=False -> ficou divergência, o ajuste não se aplica ou falhou: fica para a Contas a Pagar."""
    session = requests.Session()
    alerta = _auditar_fase2_financeiro(session, doc_num, doc_year, company_code, dados_nf)
    res_f3, texto, ok = {}, "", True
    if alerta:
        # Parcela(s) já paga(s) antes de o card chegar em Pagamento: a auditoria só olha as partidas em aberto e
        # acusa "3 parcelas no SAP x 4 no XML" (card 1441815143, NF 54058: parcela 1 paga pelo ZP 2059271091).
        # Se TODAS as parcelas do lançamento (pagas ou não) são iguais ao XML, não há o que ajustar.
        pagas = _parcelas_pagas_iguais_ao_xml(doc_num, doc_year, company_code, dados_nf)
        if pagas:
            texto = (f"✅ Parcelas iguais à NF: nada a ajustar (parcela(s) {', '.join(map(str, pagas))} já paga(s) "
                     f"antes da entrada em Pagamento).")
            alerta = ""
    if alerta:
        if not (FASE3_AJUSTAR_DATAS or FASE3_REPARCELAR):
            return {"ok": False, "ajustou": False, "texto": alerta}
        res_f3 = _fase3_ajustar_parcelas(doc_num, doc_year, company_code, dados_nf, card_data)
        if not res_f3:
            if _parcelas_iguais_ao_xml(doc_num, doc_year, company_code, dados_nf, card_data):
                # ex.: só o aviso de "Vencimento Precoce (Mora)" de NF lançada com a data do XML já passada
                return {"ok": True, "ajustou": False,
                        "texto": "✅ Parcelas iguais à NF: nada a ajustar (o alerta da auditoria é só de vencimento já passado)."}
            return {"ok": False, "ajustou": False,
                    "texto": alerta + "\n⚠️ Ajuste automático não se aplica a este caso (ver regras da Fase 3)."}
        restante = _auditar_fase2_financeiro(session, doc_num, doc_year, company_code, dados_nf)
        # Depois de a própria automação pôr o vencimento do XML (já passado), a reauditoria acusa só "Vencimento
        # Precoce (Mora)". Isso não é pendência: a parcela está igual à NF (NF 4350, NF 7347 e NF 502, 19-20/10).
        if restante and _so_alerta_de_mora(restante) and _parcelas_iguais_ao_xml(doc_num, doc_year, company_code,
                                                                                   dados_nf, card_data):
            restante = ""
        texto = "\n\n".join(t for t in (_texto_fase3(res_f3, doc_year), restante) if t)
        comp = res_f3.get("comp_adto") or {}
        if (res_f3.get("falha") or restante or res_f3.get("problemas")
                or (res_f3.get("antecipada") and comp and not comp.get("ok"))):
            return {"ok": False, "ajustou": True, "texto": texto}
    res_adto = []
    if FASE3_ADIANTAMENTO_CARD:
        res_adto = _compensar_adiantamentos_card(doc_num, doc_year, company_code, dados_nf, card_data, res_f3)
        texto = "\n\n".join(t for t in (texto, _texto_adiantamentos_card(res_adto)) if t)
        ok = all(r["ok"] for r in res_adto)
    return {"ok": ok, "ajustou": bool(res_f3) or bool(res_adto), "texto": texto}


def _texto_adiantamentos_card(lst: list) -> str:
    if not lst:
        return ""
    linhas = []
    for r in lst:
        base = (f"• Parcela {r['n']} (R$ {r['valor']:.2f}, venc. {r['venc'].strftime('%d/%m/%Y') if r['venc'] else '?'}) "
                f"x adiantamento doc. {r['adto']} \"{r['texto_adto']}\"")
        linhas.append(f"{base}: compensada (doc. {r['doc_comp']})." if r["ok"]
                      else f"{base}: ⚠️ compensação não concluída ({r['falha']}). Compensar manualmente.")
    if all(r["ok"] for r in lst):
        return ("✅ *Adiantamento(s) do card compensado(s) automaticamente (Contas a Pagar):*\n" + "\n".join(linhas)
                + "\n_Apenas conferir._")
    return ("⚠️ *Adiantamento(s) do card: compensação automática não concluída (Contas a Pagar):*\n"
            + "\n".join(linhas))


def _texto_fase3(res: dict, doc_year: str) -> str:
    txt = ""
    if res.get("tipo") == "valor":
        if res.get("doc_novo") and not res.get("falha"):
            if res.get("antecipada"):
                linhas = [f"• Parcela {i}: R$ {v:.2f} (venc. {dt.strftime('%d/%m/%Y')})"
                          + (f" -- BLOQUEADA ({b}): já paga por adiantamento" if i == 1 else "")
                          for i, v, dt, b in res.get("novas", [])]
                comp = res.get("comp_adto") or {}
                if comp.get("ok"):
                    linhas.append(f"✅ Parcela 1 compensada automaticamente com o adiantamento doc. "
                                  f"{res.get('adiantamento')} (compensação doc. {comp.get('doc_comp')}).")
                else:
                    if comp.get("falha"):
                        linhas.append(f"⚠️ Compensação automática com o adiantamento não concluída: {comp['falha']}")
                    linhas.append("➡️ Compensar a parcela 1 com o adiantamento "
                                  + (f"doc. {res['adiantamento']}." if res.get("adiantamento")
                                     else "(não identificado com segurança -- localizar no fornecedor)."))
            else:
                linhas = [f"• Parcela {i}: R$ {de:.2f} → R$ {para:.2f} (venc. {dt.strftime('%d/%m/%Y')})"
                          for i, de, para, dt in res.get("mudancas", []) if de != para]
            vencidas = [str(i) for i, _v, dt, _b in res.get("novas", [])
                        if dt < datetime.now(timezone.utc).date() and not (res.get("antecipada") and i == 1)]
            if vencidas:
                linhas.append(f"ℹ️ Parcela(s) {', '.join(vencidas)} com vencimento já passado, igual à NF.")
            if res.get("retencao"):
                linhas.append(f"ℹ️ A última parcela inclui R$ {res['retencao']:.2f} de impostos retidos na NF "
                              f"(duplicata do XML + retenção).")
            txt = ("✅ *Valor das parcelas ajustado automaticamente (Contas a Pagar):*\n"
                   f"📄 Lançamento original: {res.get('acc_doc')} / {doc_year} (parcelas compensadas pelo doc. {res.get('doc_comp')})\n"
                   f"📄 Parcelas novas: documento {res.get('doc_novo')} ({res.get('tipo_doc') or 'RE'})\n"
                   + "\n".join(linhas)
                   + "\n_Apenas conferir._")
            if res.get("problemas"):
                txt += "\n⚠️ Conferência das parcelas novas: " + "; ".join(res["problemas"])
        else:
            txt = (f"⚠️ *Ajuste automático do valor das parcelas não concluído:* {res.get('falha')}\n"
                   "_Ajuste manual necessário no SAP._")
        return txt

    linhas = [f"• Parcela {p} (item {item}): {de.strftime('%d/%m/%Y') if de else '?'} → {para.strftime('%d/%m/%Y')}"
              for p, item, de, para in res.get("ajustados", [])]
    if linhas:
        txt += ("✅ *Vencimento ajustado automaticamente (Contas a Pagar):*\n"
                f"📄 Lançamento Contábil: {res.get('acc_doc')} / Exercício {doc_year}\n"
                + "\n".join(linhas)
                + ("\nℹ️ Vencimento já passado, igual à NF." if any(
                    para < datetime.now(timezone.utc).date() for _p, _i, _de, para in res.get("ajustados", [])) else "")
                + "\n_Sem documento novo; a condição de pagamento dessas parcelas foi removida. Apenas conferir._")
    if res.get("falha"):
        txt += ("\n" if txt else "") + (f"⚠️ *Ajuste automático de vencimento não concluído:* {res['falha']}\n"
                                          "_Ajuste manual necessário no SAP._")
    return txt


# ─────────────────────────────────────────────────────────────────────────────
#  Funções de Auditoria Pós-Lançamento (Documento Fiscal / J1B3N)
# ─────────────────────────────────────────────────────────────────────────────

def _buscar_nf_do_documento(session: requests.Session, doc_num: str, doc_year: str) -> str:
    url = f"{BASE}/A_SupplierInvoice(SupplierInvoice='{doc_num}',FiscalYear='{doc_year}')/to_BR_SupplierInvoiceNFDocument?$format=json"
    try:
        resp = session.get(url, headers={**_auth_headers(), "Accept": "application/json"}, **_auth_kwargs(), timeout=15)
        if resp.ok:
            return resp.json().get("d", {}).get("BR_NotaFiscal", "")
        else:
            log.warning(f"Erro SAP ao buscar NF do doc {doc_num}: {resp.text}")
    except Exception as e:
        log.warning(f"Erro de rede ao buscar número da NF associada ao documento {doc_num}: {e}")
    return ""

def _buscar_dados_fiscais_cds(session: requests.Session, nf_doc: str) -> list:
    if not nf_doc: return []
    
    nf_doc_padded = str(nf_doc).strip().zfill(10)
    url = f"{BASE_CDS_TAX}/YY1_NF_ITEM_TAX_CDS?$filter=BR_NotaFiscal eq '{nf_doc_padded}'&$format=json"
    
    import time
    for _ in range(3):
        try:
            resp = session.get(url, headers={**_auth_headers(), "Accept": "application/json"}, **_auth_kwargs(), timeout=15)
            if resp.ok:
                resultados = resp.json().get("d", {}).get("results", [])
                if resultados:
                    return resultados
        except Exception as e:
            log.warning(f"Erro de rede ao buscar CDS View da NF {nf_doc_padded}: {e}")
        time.sleep(2)
        
    return []

def _patch_com_retry(session, url, payload, headers, max_tentativas=5, espera_segundos=8):
    """
    Faz PATCH com algumas tentativas e espera entre elas. O SAP costuma recusar a atualizacao
    do documento fiscal por alguns segundos logo apos o PostInvoice (provavel replicacao/consistencia
    interna entre a Fatura recem-contabilizada e o documento fiscal), mesmo que o mesmo campo seja
    editavel manualmente minutos depois. Mesma logica de tolerancia a atraso ja usada na Fase 2 e na
    busca da CDS fiscal -- so que aqui como retry de escrita, nao de leitura.
    """
    ultima_resposta = None
    for tentativa in range(1, max_tentativas + 1):
        resp = session.patch(url, json=payload, headers=headers, **_auth_kwargs(), timeout=15)
        if resp.ok:
            if tentativa > 1:
                log.info(f"PATCH em {url} funcionou na tentativa {tentativa} de {max_tentativas}.")
            return True, resp
        ultima_resposta = resp
        if tentativa < max_tentativas:
            time.sleep(espera_segundos)
    return False, ultima_resposta


CTE_ATUALIZAR_CAMPOS = _os.getenv("CTE_ATUALIZAR_CAMPOS", "0").strip() == "1"


def _atualizar_cte_sap(session: requests.Session, nf_doc: str, jur_ini: str, jur_fim: str, freight_nature: str):
    nf_doc_padded = str(nf_doc).strip().zfill(10)
    headers = {**_auth_headers(), "Content-Type": "application/json", "Accept": "application/json", "x-csrf-token": _fetch_csrf_token(session)}
    
    # Atualiza o Cabeçalho (Jurisdição)
    if jur_ini or jur_fim:
        payload_hdr = {}
        if jur_ini: payload_hdr["BR_CTeJurisdictionCodeOrigin"] = jur_ini
        if jur_fim: payload_hdr["BR_CTeJurisdictionCodeDest"] = jur_fim
        
        url_hdr = f"{BASE_NF}/A_BR_NFDocument('{nf_doc_padded}')"
        ok_hdr, r_hdr = _patch_com_retry(session, url_hdr, payload_hdr, headers)
        if not ok_hdr:
            status = r_hdr.status_code if r_hdr is not None else "sem resposta"
            texto = r_hdr.text if r_hdr is not None else ""
            log.warning(f"Falha ao atualizar Domicílios Fiscais do CT-e {nf_doc_padded} após novas tentativas (HTTP {status}): {texto}")
            
    # Atualiza o Item 10 (Natureza do Frete)
    if freight_nature:
        payload_itm = {"BR_NFFreightNature": freight_nature}
        url_itm = f"{BASE_NF}/A_BR_NFItem(BR_NotaFiscal='{nf_doc_padded}',BR_NotaFiscalItem='000010')"
        ok_itm, r_itm = _patch_com_retry(session, url_itm, payload_itm, headers)
        if not ok_itm:
            status = r_itm.status_code if r_itm is not None else "sem resposta"
            texto = r_itm.text if r_itm is not None else ""
            log.warning(f"Falha ao atualizar Natureza do Frete do CT-e {nf_doc_padded} após novas tentativas (HTTP {status}): {texto}")

# ─────────────────────────────────────────────────────────────────────────────
#  Buscador Multi-Pedidos + MIGO + Pricing + FIXED ASSETS (OData V4)
# ─────────────────────────────────────────────────────────────────────────────

def buscar_cnpj_fornecedor(codigo_fornecedor: str) -> str:
    bp_limpo = str(codigo_fornecedor).strip().zfill(10)
    if not bp_limpo or bp_limpo == "0000000000": return ""
        
    base_bp = SAP_BASE_URL.rstrip("/") + "/sap/opu/odata/sap/API_BUSINESS_PARTNER"
    try:
        session = requests.Session()
        headers, kwargs = {**_auth_headers(), "Accept": "application/json"}, _auth_kwargs()
        url = f"{base_bp}/A_BusinessPartnerTaxNumber(BusinessPartner='{bp_limpo}',BPTaxType='BR1')"
        
        r = session.get(url, headers=headers, **kwargs, timeout=15)
        if r.ok:
            data = r.json().get("d", {})
            cnpj = str(data.get("BPTaxNumber", "")).strip()
            return cnpj if cnpj else str(data.get("BPTaxLongNumber", "")).strip()
    except Exception as e:
        log.warning("Erro de rede ao buscar CNPJ do Fornecedor %s: %s", bp_limpo, e)
    return ""

def _preencher_datas_remessa(session, base_po, headers, kwargs, po_limpo: str, mapa_datas_remessa: dict):
    """
    Best-effort: lê as datas de remessa (divisões do item) do Pedido e guarda a mais antiga de
    cada item em mapa_datas_remessa['<pedido>-<item>'] (ms). Usada só como DESEMPATE entre itens
    idênticos. Qualquer falha é ignorada (sem data, o desempate volta a ser o nº do item).
    """
    try:
        skip = 0
        while True:
            url = (f"{base_po}/A_PurchaseOrderScheduleLine?$format=json"
                   f"&$filter=PurchasingDocument eq '{po_limpo}'"
                   f"&$select=PurchasingDocument,PurchasingDocumentItem,ScheduleLine,ScheduleLineDeliveryDate"
                   f"&$top=100&$skip={skip}")
            r = session.get(url, headers=headers, **kwargs, timeout=15)
            if not r.ok:
                log.warning("Não foi possível ler as datas de remessa do Pedido %s (HTTP %s). "
                            "Desempate seguirá pelo nº do item.", po_limpo.lstrip("0"), r.status_code)
                return
            linhas = r.json().get("d", {}).get("results", [])
            for ln in linhas:
                m = re.search(r"/Date\((-?\d+)", str(ln.get("ScheduleLineDeliveryDate") or ""))
                if not m:
                    continue
                chave = f"{po_limpo}-{str(ln.get('PurchasingDocumentItem', '')).zfill(5)}"
                ms = int(m.group(1))
                if chave not in mapa_datas_remessa or ms < mapa_datas_remessa[chave]:
                    mapa_datas_remessa[chave] = ms
            if len(linhas) < 100:
                break
            skip += 100
    except Exception as e:
        log.warning("Falha ao ler datas de remessa do Pedido %s: %s", po_limpo, e)


def _preencher_referencia_migos(session, base_migo, headers, kwargs, mapa_migo_global: dict, po_limpo: str):
    """
    Best-effort: busca a Referência (nota de remessa, ex.: '240934-70') do cabeçalho de cada
    recebimento do Pedido e grava em entry['ref']. Serve só para PREFERIR o recebimento da
    própria NF em _selecionar_referencia_migo. Qualquer falha é ignorada (fica sem 'ref' e o
    comportamento é o de antes).
    """
    try:
        entradas = [e for k, lst in mapa_migo_global.items() if k.startswith(f"{po_limpo}-") for e in lst]
        docs = sorted({(e["doc"], e["year"]) for e in entradas if e.get("doc")})
        if not docs:
            return
        refs = {}
        for i in range(0, len(docs), 15):
            bloco = docs[i:i + 15]
            filtro = " or ".join(
                f"(MaterialDocument eq '{d}' and MaterialDocumentYear eq '{y}')" for d, y in bloco
            )
            url = (f"{base_migo}/A_MaterialDocumentHeader?$format=json&$filter={filtro}"
                   f"&$select=MaterialDocument,MaterialDocumentYear,ReferenceDocument")
            r = session.get(url, headers=headers, **kwargs, timeout=15)
            if not r.ok:
                log.warning("Não foi possível ler a referência dos recebimentos (HTTP %s). "
                            "Seguindo sem preferência por referência.", r.status_code)
                return
            for h in r.json().get("d", {}).get("results", []):
                refs[(str(h.get("MaterialDocument", "")), str(h.get("MaterialDocumentYear", "")))] = \
                    str(h.get("ReferenceDocument", "") or "").strip()
        for e in entradas:
            e["ref"] = refs.get((e["doc"], e["year"]), "")
    except Exception as e:
        log.warning("Falha ao buscar referência dos recebimentos: %s", e)


def _buscar_centro_do_ativo(session: requests.Session, company_code: str, master_fixed_asset: str, cache: dict) -> str:
    asset_limpo = str(master_fixed_asset).strip()
    if not asset_limpo: return ""
    asset_pad = asset_limpo.zfill(12)
    
    cache_key = f"{company_code}-{asset_pad}"
    if cache_key in cache: return cache[cache_key]
    
    base_asset = SAP_BASE_URL.rstrip("/") + "/sap/opu/odata4/sap/api_fixedasset/srvd_a2x/sap/fixedasset/0001"
    url = f"{base_asset}/FixedAssetAssignment?$filter=CompanyCode eq '{company_code}' and MasterFixedAsset eq '{asset_pad}'&$select=Plant"
    
    try:
        headers = {**_auth_headers(), "Accept": "application/json"}
        r = session.get(url, headers=headers, **_auth_kwargs(), timeout=15)
        if r.ok:
            data = r.json().get("value", [])
            if data:
                plant = str(data[0].get("Plant", "")).strip()
                cache[cache_key] = plant
                return plant
            
    except Exception as e:
        log.warning(f"Erro ao buscar Centro do Ativo {asset_pad}: {e}")
        
    cache[cache_key] = ""
    return ""

def buscar_empresa_por_po(numeros_po: str) -> dict:
    pedidos = re.findall(r"\d+", numeros_po)
    if not pedidos: return {}
    
    base_po = SAP_BASE_URL.rstrip("/") + "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV"
    base_migo = SAP_BASE_URL.rstrip("/") + "/sap/opu/odata/sap/API_MATERIAL_DOCUMENT_SRV"
    resultado = {"itens_po": []}
    
    try:
        session = requests.Session()
        headers, kwargs = {**_auth_headers(), "Accept": "application/json"}, _auth_kwargs()
        
        raw_itens_total = []
        mapa_migo_global = {} 
        mapa_pricing_global = {} 
        mapa_condicoes_pgto = {}
        mapa_account_assignment = {}
        mapa_projeto_acc = {}  # item do pedido -> PEP / rede (classificação contábil de projeto)
        mapa_datas_remessa = {}
        cache_ativos = {}

        for idx, po in enumerate(pedidos):
            po_limpo = po.zfill(10)
            if po_limpo == "0000000000": continue

            r_hdr = session.get(f"{base_po}/A_PurchaseOrder('{po_limpo}')", params={"$format": "json", "$select": "PurchaseOrder,CompanyCode,Supplier,DocumentCurrency,PaymentTerms"}, headers=headers, **kwargs, timeout=15)
            if r_hdr.ok:
                hdr = r_hdr.json().get("d", {})
                fornecedor_id = hdr.get("Supplier", "")
                cond_pgto = str(hdr.get("PaymentTerms", "")).strip()
                
                if cond_pgto:
                    mapa_condicoes_pgto[po.lstrip("0")] = cond_pgto

                if idx == 0:
                    resultado.update({
                        "company_code": hdr.get("CompanyCode", ""), 
                        "supplier": fornecedor_id, 
                        "payment_terms": cond_pgto
                    })
                    if fornecedor_id:
                        resultado["cnpj_fornecedor_sap"] = buscar_cnpj_fornecedor(fornecedor_id)

            skip_pricing = 0
            top_pricing = 50
            url_base_pricing = f"{SAP_BASE_URL.rstrip('/')}/sap/opu/odata/sap/YY1_API_PO_PRICING_CDS/YY1_API_PO_Pricing?$format=json&$filter=PurchaseOrder eq '{po_limpo}'"
            
            try:
                while True:
                    url_pricing = f"{url_base_pricing}&$top={top_pricing}&$skip={skip_pricing}"
                    r_pricing = session.get(url_pricing, headers=headers, **kwargs, timeout=15)
                    
                    if r_pricing.ok:
                        pricing_results = r_pricing.json().get("d", {}).get("results", [])
                        if not pricing_results: break
                        
                        for cond in pricing_results:
                            item_num = str(cond.get("PurchaseOrderItem", "")).lstrip("0").zfill(5)
                            c_type = cond.get("ConditionType", "").upper()
                            c_amt = float(cond.get("ConditionAmount", 0))
                            tax_code = str(cond.get("TaxCode", "")).strip()
                            
                            chave_pricing = f"{po_limpo}-{item_num}"
                            
                            if chave_pricing not in mapa_pricing_global:
                                mapa_pricing_global[chave_pricing] = {"bruto_esperado": 0.0, "fretes": [], "tax_code": None, "impostos_valores": {}}
                                
                            if tax_code and not mapa_pricing_global[chave_pricing]["tax_code"]:
                                mapa_pricing_global[chave_pricing]["tax_code"] = tax_code
                            
                            if c_type in ["PMP0", "ZD01", "ZO01"]:
                                mapa_pricing_global[chave_pricing]["bruto_esperado"] += c_amt
                                
                            elif c_type == "ZNF1": 
                                mapa_pricing_global[chave_pricing]["fretes"].append({
                                    "amount": c_amt,
                                    "step": str(cond.get("PricingProcedureStep", "803")).strip().zfill(3),
                                    "counter": str(cond.get("PricingProcedureCounter", "1")).strip().zfill(3),
                                    "supplier": cond.get("FreightSupplier", "")
                                })
                            
                            if c_type in ["ZICM", "ZPIS", "ZCOF", "ZIPI"]:
                                mapa_pricing_global[chave_pricing]["impostos_valores"][c_type] = c_amt

                            # ZYO1 = PIS/COFINS (e ICMS) do frete que o Pedido tira do ZNF1
                            # (ZNF1 + ZYO1 = ZF01 = frete da nota). Guardado para devolver ao frete abaixo.
                            if c_type == "ZYO1":
                                mapa_pricing_global[chave_pricing]["frete_imposto"] = (
                                    mapa_pricing_global[chave_pricing].get("frete_imposto", 0.0) + c_amt
                                )
                        
                        if len(pricing_results) < top_pricing: break
                        skip_pricing += top_pricing
                    else:
                        break
            except Exception as e:
                log.warning(f"Erro de conexão ao buscar Pricing da PO {po_limpo}: {e}")

            # Frete planejado vai pelo valor BRUTO (ZNF1 + ZYO1 = frete da nota).
            # O ZNF1 do Pedido já vem sem o PIS/COFINS do frete (ZYO1), mas esse mesmo imposto
            # também já é descontado do item (ZPIS/ZCOF têm o frete na base). Mandando o ZNF1
            # líquido, o imposto do frete saía duas vezes e a fatura ficava sem saldo zero
            # (NF 1654: 10,21; NF 2630 e 45763 só passaram porque a autocorreção jogou a
            # diferença no item). Só mexe quando o item tem exatamente 1 ZNF1 e um ZYO1.
            for _chave_p, _dados_p in mapa_pricing_global.items():
                if not _chave_p.startswith(f"{po_limpo}-"):
                    continue
                _imp_frete = float(_dados_p.get("frete_imposto", 0.0) or 0.0)
                _fretes_p = _dados_p.get("fretes", [])
                if abs(_imp_frete) > 0.005 and len(_fretes_p) == 1 and "amount_pedido" not in _fretes_p[0]:
                    _fretes_p[0]["amount_pedido"] = _fretes_p[0]["amount"]
                    _fretes_p[0]["amount"] = round(_fretes_p[0]["amount"] + _imp_frete, 2)

            # Busca de Itens do Pedido
            skip_itm = 0
            top_itm = 50
            while True:
                url_itm = f"{base_po}/A_PurchaseOrderItem?$format=json&$filter=PurchaseOrder eq '{po_limpo}'&$select=PurchaseOrder,PurchaseOrderItem,Plant,PurchaseOrderQuantityUnit,OrderPriceUnit,AccountAssignmentCategory,TaxCode,BR_MaterialUsage,Material,PurchaseOrderItemText,OrderQuantity,NetPriceAmount,NetPriceQuantity,ConsumptionTaxCtrlCode,InvoiceIsGoodsReceiptBased,IsFinallyInvoiced&$top={top_itm}&$skip={skip_itm}"
                r_itm = session.get(url_itm, headers=headers, **kwargs, timeout=15)
                if r_itm.ok:
                    data_itm = r_itm.json().get("d", {}).get("results", [])
                    if not data_itm: break
                    raw_itens_total.extend(data_itm)
                    if len(data_itm) < top_itm: break
                    skip_itm += top_itm
                else: break
                
            # Busca da Classificação Contábil
            skip_acc = 0
            top_acc = 50
            while True:
                url_acc = f"{base_po}/A_PurOrdAccountAssignment?$format=json&$filter=PurchaseOrder eq '{po_limpo}'&$select=PurchaseOrder,PurchaseOrderItem,MasterFixedAsset,WBSElementExternalID,ProjectNetwork,CostCenter,GLAccount&$top={top_acc}&$skip={skip_acc}"
                r_acc = session.get(url_acc, headers=headers, **kwargs, timeout=15)
                if r_acc.ok:
                    acc_results = r_acc.json().get("d", {}).get("results", [])
                    if not acc_results: break
                    for acc in acc_results:
                        item_po_acc = str(acc.get("PurchaseOrderItem", "")).zfill(5)
                        chave_acc = f"{po_limpo}-{item_po_acc}"
                        if chave_acc not in mapa_account_assignment or not mapa_account_assignment[chave_acc].get("MasterFixedAsset"):
                            mapa_account_assignment[chave_acc] = acc
                        info_prj = mapa_projeto_acc.setdefault(chave_acc, {"pep": "", "rede": "", "centro_custo": "", "conta": "", "ccs": []})
                        _cc_acc = str(acc.get("CostCenter") or "").strip()
                        if _cc_acc and _cc_acc not in info_prj["ccs"]:
                            info_prj["ccs"].append(_cc_acc)   # todos os centros de custo (classificação múltipla)
                        for campo, origem in (("pep", "WBSElementExternalID"), ("rede", "ProjectNetwork"),
                                              ("centro_custo", "CostCenter"), ("conta", "GLAccount")):
                            if not info_prj[campo]:
                                info_prj[campo] = str(acc.get(origem) or "").strip()
                    if len(acc_results) < top_acc: break
                    skip_acc += top_acc
                else: break
                
            _preencher_datas_remessa(session, base_po, headers, kwargs, po_limpo, mapa_datas_remessa)

            # Busca de MIGO
            skip_migo = 0
            top_migo = 50
            try:
                while True:
                    url_migo = f"{base_migo}/A_MaterialDocumentItem?$format=json&$filter=PurchaseOrder eq '{po_limpo}'&$select=MaterialDocument,MaterialDocumentYear,MaterialDocumentItem,PurchaseOrderItem,GoodsMovementType,GoodsMovementIsCancelled,QuantityInBaseUnit,MaterialBaseUnit&$top={top_migo}&$skip={skip_migo}"
                    r_migo = session.get(url_migo, headers=headers, **kwargs, timeout=15)
                    if r_migo.ok:
                        migo_results = r_migo.json().get("d", {}).get("results", [])
                        if not migo_results: break
                        
                        for m in migo_results:
                            if m.get("GoodsMovementType") == "101" and not m.get("GoodsMovementIsCancelled"):
                                item_po_migo = str(m.get("PurchaseOrderItem", "")).zfill(5)
                                chave_migo = f"{po_limpo}-{item_po_migo}"
                                
                                if chave_migo not in mapa_migo_global:
                                    mapa_migo_global[chave_migo] = []
                                    
                                mapa_migo_global[chave_migo].append({
                                    "doc": str(m.get("MaterialDocument", "")),
                                    "year": str(m.get("MaterialDocumentYear", "")),
                                    "item": str(m.get("MaterialDocumentItem", "")),
                                    "qty": float(m.get("QuantityInBaseUnit", 0) or 0),
                                    "unit": str(m.get("MaterialBaseUnit", "")).strip()
                                })
                                
                        if len(migo_results) < top_migo: break
                        skip_migo += top_migo
                    else: break
            except Exception as e:
                log.warning("Falha ao buscar MIGO: %s", e)

            _preencher_referencia_migos(session, base_migo, headers, kwargs, mapa_migo_global, po_limpo)

        # Não barramos o card aqui, apenas injetamos as possibilidades para o Motor (Fase 1) avaliar
        condicoes_unicas = list(set(mapa_condicoes_pgto.values()))
        resultado["condicoes_encontradas"] = condicoes_unicas

        raw_itens_total.sort(key=lambda x: x.get("PurchaseOrderItem", ""))
        for item in raw_itens_total:
            if item.get("IsFinallyInvoiced") is True:
                continue

            mat_id = str(item.get("Material", "")).strip()
            po_num = str(item.get("PurchaseOrder", "")).zfill(10)
            item_num = str(item.get("PurchaseOrderItem", "")).zfill(5)
            
            chave_busca = f"{po_num}-{item_num}"
            
            lista_migos_disponiveis = mapa_migo_global.get(chave_busca, [])
            unit_pedido_migo = str(item.get("PurchaseOrderQuantityUnit", "UN")).strip()
            for _migo_entry in lista_migos_disponiveis:
                try:
                    _migo_entry["qty"] = converter_quantidade(
                        _migo_entry.get("qty", 0.0), _migo_entry.get("unit", "UN") or "UN", unit_pedido_migo
                    )
                except ValueError as _e_unid_migo:
                    log.warning(
                        "Não foi possível converter a unidade do recebimento (%s) para a unidade do Pedido "
                        "(%s) no item %s-%s: %s. Esse recebimento não entrará na soma de quantidade recebida.",
                        _migo_entry.get("unit"), unit_pedido_migo, po_num.lstrip("0"), item_num, _e_unid_migo
                    )
                    _migo_entry["qty"] = 0.0
            qtd_recebida_total = sum(float(_m.get("qty", 0.0) or 0.0) for _m in lista_migos_disponiveis)
            pricing_data = mapa_pricing_global.get(chave_busca, {})
            acc_info = mapa_account_assignment.get(chave_busca, {})
            
            ncm_do_pedido = str(item.get("ConsumptionTaxCtrlCode", "")).replace(".", "").strip()
            gr_based_iv = item.get("InvoiceIsGoodsReceiptBased", False)
            
            bruto_sap = pricing_data.get("bruto_esperado", 0.0)
            lista_fretes = pricing_data.get("fretes", [])
            tax_code_pricing = pricing_data.get("tax_code")
            impostos_sap_valores = pricing_data.get("impostos_valores", {})
            
            if bruto_sap <= 0:
                qtd_pedida = float(item.get("OrderQuantity", 0))
                preco_liq  = float(item.get("NetPriceAmount", 0))
                unid_preco = float(item.get("NetPriceQuantity", 1) or 1)
                bruto_sap = (qtd_pedida / unid_preco) * preco_liq
            
            tax_code_final = tax_code_pricing if tax_code_pricing else str(item.get("TaxCode", "")).strip()
            
            master_fixed_asset = str(acc_info.get("MasterFixedAsset", "")).strip()
            asset_plant = ""
            if master_fixed_asset and resultado.get("company_code"):
                asset_plant = _buscar_centro_do_ativo(session, resultado["company_code"], master_fixed_asset, cache_ativos)

            resultado["itens_po"].append({
                "po_number":    po_num.lstrip("0"),
                "po_item":      item_num,
                "plant":        str(item.get("Plant", "")).strip(), 
                "master_fixed_asset": master_fixed_asset, 
                "asset_plant":  asset_plant, 
                "material_usage": str(item.get("BR_MaterialUsage", "")).strip(),
                "account_category": str(item.get("AccountAssignmentCategory", "")).strip(),
                "projeto_pep": mapa_projeto_acc.get(chave_busca, {}).get("pep", ""),
                "projeto_rede": mapa_projeto_acc.get(chave_busca, {}).get("rede", ""),
                "centro_custo": mapa_projeto_acc.get(chave_busca, {}).get("centro_custo", ""),
                "centros_custo": list(mapa_projeto_acc.get(chave_busca, {}).get("ccs", [])),
                "conta_contabil": mapa_projeto_acc.get(chave_busca, {}).get("conta", ""),
                "unit":         str(item.get("PurchaseOrderQuantityUnit", "UN")).strip(),
                "po_price_unit": str(item.get("OrderPriceUnit", "")).strip(),
                "tax_code":     tax_code_final,
                "material":     mat_id,
                "ncm_cadastro": ncm_do_pedido,
                "descricao":    str(item.get("PurchaseOrderItemText", "")),
                "qtd_pedida":   float(item.get("OrderQuantity", 0)),
                "preco_unit":   float(item.get("NetPriceAmount", 0)),
                "unid_preco":   float(item.get("NetPriceQuantity", 1) or 1), 
                "bruto_sap_esperado": bruto_sap,
                "impostos_sap": impostos_sap_valores, 
                "fretes_planejados": lista_fretes,
                "lista_migos_disponiveis": lista_migos_disponiveis, 
                "qtd_recebida_total": qtd_recebida_total, 
                "data_remessa": mapa_datas_remessa.get(chave_busca),
                "gr_based_iv":  gr_based_iv  
            })
        
        log.info("Lidos %d itens disponíveis de %d pedido(s).", len(resultado["itens_po"]), len(pedidos))
    except ValueError:
        raise
    except Exception as e:
        log.warning("Erro ao buscar POs %s: %s", numeros_po, e)

    return resultado

# ─────────────────────────────────────────────────────────────────────────────
#  Motor de Payload Enterprise (1:1 e Fallback 1:N)
# ─────────────────────────────────────────────────────────────────────────────

OPERACOES_SEM_VALIDACAO_CONDICAO = {"compra_site", "adiantamento", "agenciamento", "cartoes", "cte"}


def _operacoes_do_card(card_data: dict) -> set:
    """
    Conjunto de operações do card (todas as etiquetas, em qualquer posição -- ver
    pipefy_client.buscar_card). Mantém compatibilidade com quem só manda tipo_operacao
    (ex.: endpoint do App).
    """
    ops = set(card_data.get("operacoes") or [])
    tipo = card_data.get("tipo_operacao")
    if tipo and tipo != "normal":
        ops.add(tipo)
    return ops


def _item_tem_recebimento_da_nf(po_item: dict, nf_numero, nf_serie) -> bool:
    """True se algum recebimento (MIGO) deste item do Pedido tem como Referência a própria NF."""
    return any(
        _referencia_migo_bate_com_nf(m.get("ref", ""), nf_numero, nf_serie)
        for m in (po_item.get("lista_migos_disponiveis") or [])
    )


def _chave_data_remessa(po_item: dict) -> int:
    """Data de remessa mais antiga do item (ms); sem data conhecida, vai para o fim da fila."""
    try:
        v = po_item.get("data_remessa")
        return int(v) if v is not None else 10**15
    except (TypeError, ValueError):
        return 10**15


def _item_indicado_no_xml(item_nf: dict, po_item: dict, pedidos_do_card: set) -> bool:
    """
    True quando o XML da NF (tags xPed/nItemPed) aponta explicitamente para este item do Pedido.
    Usado só como PRIORIDADE no matching: se o XML não trouxer a informação, ou ela não bater
    com nenhum item disponível, o motor segue exatamente como antes (NCM + valor/preço).
    Caso real: NF 472550 / Pedido 4523612059 -- o pedido repete os mesmos materiais e preços
    em lotes diferentes (itens 10-60, 70-150, 170-240) e, sem essa prioridade, o empate era
    resolvido pelo item mais antigo, já faturado (erro SAP 8B/246).
    """
    item_xml = re.sub(r"\D", "", str(item_nf.get("item_pedido_xml", "") or ""))
    if not item_xml:
        return False
    try:
        if int(item_xml) != int(str(po_item.get("po_item", "0")).strip() or 0):
            return False
    except ValueError:
        return False
    pedido_xml = re.sub(r"\D", "", str(item_nf.get("pedido_xml", "") or "")).lstrip("0")
    if pedido_xml:
        return pedido_xml == str(po_item.get("po_number", "")).lstrip("0")
    # Sem xPed: só confia no número do item se o card tiver um único pedido
    return len(pedidos_do_card) == 1


def _referencia_migo_bate_com_nf(ref_migo: str, nf_numero, nf_serie) -> bool:
    """
    Compara a Referência do recebimento (campo 'Nota de remessa'/XBLNR, ex.: '240934-70')
    com o número/série da NF. Tolerante a zeros à esquerda; se a referência não tiver série,
    compara só o número.
    """
    ref = str(ref_migo or "").strip()
    numero = str(nf_numero or "").strip().lstrip("0")
    if not ref or not numero:
        return False
    partes = [p for p in re.split(r"[-/\s]+", ref) if p]
    if not partes:
        return False
    if re.sub(r"\D", "", partes[0]).lstrip("0") != numero:
        return False
    serie = str(nf_serie or "").strip().lstrip("0")
    if len(partes) > 1 and serie:
        serie_ref = re.sub(r"\D", "", partes[1]).lstrip("0")
        if serie_ref and serie_ref != serie:
            return False
    return True


def _selecionar_referencia_migo(migos_disponiveis, po_number, po_item_id, tipo_op, migo_ultimo_usado_por_item, chave_migo_banco, nf_numero=None, nf_serie=None):
    """
    Escolhe a referência de MIGO (doc/ano/item) para uma linha de fatura com recebimento
    obrigatório (gr_based_iv). Consome (pop) a próxima referência disponível; se a lista
    estiver vazia, reaproveita a última referência já usada para este item de Pedido dentro
    desta mesma fatura -- isso é o que permite que 1 recebimento físico cubra várias linhas
    da NF quando o Pedido tem a quantidade dividida em mais de uma linha (caso real: NF
    189977 / Pedido 4516549565, confirmado em produção em 21/09/2026). Só levanta erro
    quando não há absolutamente nenhuma referência, nem para reaproveitar -- comportamento
    idêntico ao que já estava validado em produção, usado agora também no matching 1:1.
    """
    if migos_disponiveis:
        # Prefere o recebimento cuja Referência (nota de remessa) é esta própria NF; se nenhum
        # bater (ou a referência não estiver disponível), mantém o comportamento anterior (1º da lista).
        idx_escolhido = next(
            (i for i, m in enumerate(migos_disponiveis)
             if _referencia_migo_bate_com_nf(m.get("ref", ""), nf_numero, nf_serie)),
            0,
        )
        if idx_escolhido > 0:
            log.info(
                f"Recebimento {migos_disponiveis[idx_escolhido].get('doc')}/{migos_disponiveis[idx_escolhido].get('item')} "
                f"escolhido para o Item {po_item_id} do Pedido {po_number} por ter a referência da própria NF "
                f"({migos_disponiveis[idx_escolhido].get('ref')})."
            )
        migo_escolhida = migos_disponiveis.pop(idx_escolhido)
        r_doc = migo_escolhida.get("doc", "")
        r_year = migo_escolhida.get("year", "")
        r_item = migo_escolhida.get("item", "")
        migo_ultimo_usado_por_item[chave_migo_banco] = {"doc": r_doc, "year": r_year, "item": r_item}
        return r_doc, r_year, r_item

    if chave_migo_banco in migo_ultimo_usado_por_item:
        migo_reaproveitada = migo_ultimo_usado_por_item[chave_migo_banco]
        r_doc = migo_reaproveitada.get("doc", "")
        r_year = migo_reaproveitada.get("year", "")
        r_item = migo_reaproveitada.get("item", "")
        log.info(
            f"Reaproveitando MIGO {r_doc}/{r_item} ja consumida nesta fatura para o Item "
            f"{po_item_id} do Pedido {po_number} (mesmo Item de PO atendendo multiplas linhas "
            f"da NF a partir de um unico recebimento fisico)."
        )
        return r_doc, r_year, r_item

    if tipo_op != "materia_prima":
        raise ValueError(
            f"Configuração de Recebimento Incorreta: O Item {po_item_id} do Pedido {po_number} não é "
            f"classificado como Matéria-Prima nesta operação, mas o Pedido exige confirmação de "
            f"recebimento (RevFatEM) antes de lançar a fatura -- isso não faz sentido para um item "
            f"de serviço/frete, que nunca vai gerar entrada de mercadoria nem planilha de serviço. "
            f"Corrija o Pedido no SAP, desmarcando essa exigência para este item."
        )
    raise ValueError(
        f"Item de entrada de mercadorias ausente para o Item {po_item_id} do Pedido {po_number}. "
        f"O SAP exige MIGO para este item."
    )


def _validar_saldo_recebimento(qtd_consumida_migo_por_item, chave_migo_banco, qty_linha, qtd_recebida_total, po_number, po_item_id):
    """
    Bloqueia o lançamento quando a quantidade que a automação está faturando para este item
    de Pedido, somada dentro desta fatura, ultrapassa o total realmente recebido (soma das
    MIGOs tipo 101 não canceladas, convertida para a unidade do Pedido). Decisão de produto
    (22/09/2026): bloquear, não só alertar -- prioridade é garantir que a automação nunca
    seja a causa direta de saldo na conta EM/EF.

    Não influencia qual referência de MIGO é escolhida (ver _selecionar_referencia_migo, que
    é independente desta função e continua sendo chamada primeiro).

    Limite conhecido, aceito conscientemente: só enxerga o que está sendo somado DENTRO desta
    fatura -- não tem visibilidade sobre quantidade já faturada em NFs anteriores lançadas em
    dias diferentes contra o mesmo item de Pedido (só o SAP sabe isso com certeza). Nesse
    cenário este bloqueio pode disparar um falso positivo -- o card vai para revisão manual
    em vez de lançar uma fatura potencialmente problemática, o que é a troca aceita.
    """
    total_antes = qtd_consumida_migo_por_item.get(chave_migo_banco, 0.0)
    total_depois = total_antes + qty_linha
    qtd_recebida_total = float(qtd_recebida_total or 0.0)

    if total_depois > qtd_recebida_total + 0.05:
        raise ValueError(
            f"Quantidade Faturada Acima do Recebido: o Item {po_item_id} do Pedido {po_number} está "
            f"somando {total_depois:.3f} nesta NF, mas o total recebido (MIGO tipo 101, não cancelado) "
            f"registrado no SAP é de apenas {qtd_recebida_total:.3f}. Lançamento bloqueado para não gerar "
            f"saldo na conta EM/EF -- confirme se falta registrar recebimento no SAP para este item, ou "
            f"se há divergência de quantidade a resolver antes de faturar."
        )

    qtd_consumida_migo_por_item[chave_migo_banco] = total_depois


_CAMPOS_SOMA_XML = ("quantidade", "valor", "vICMS", "vIPI", "vPIS", "vCOFINS")


def _limpar_xml_item_nf(card_data: dict):
    """Cada montagem de payload (inclusive as novas tentativas) começa sem a ligação NF x item do Pedido
    da tentativa anterior -- é ela que a Auditoria POP usa."""
    for p in list(card_data.get("itens_po", []) or []) + list(card_data.get("itens_po_extra", []) or []):
        p.pop("xml_item_nf", None)


def _parte_xml_item(item_nf: dict, fracao: float) -> dict:
    """Parte proporcional de uma linha da NF (quantidade, valor e impostos), para quando a linha é dividida
    entre itens do Pedido. fracao >= 1 devolve a linha como está."""
    if fracao >= 0.9999:
        return item_nf
    parte = dict(item_nf)
    for k in _CAMPOS_SOMA_XML:
        try:
            parte[k] = f"{float(item_nf.get(k) or 0) * fracao:.4f}"
        except (TypeError, ValueError):
            pass
    return parte


def _acumular_xml_item(destino: dict, item_nf: dict):
    """Liga a linha da NF ao item do Pedido (para a Auditoria POP). Quando mais de uma linha da NF cai no
    mesmo item, soma quantidade, valor e impostos -- antes ficava só a última linha e a auditoria comparava
    o item inteiro com uma linha só."""
    atual = destino.get("xml_item_nf")
    if not atual:
        destino["xml_item_nf"] = dict(item_nf)
        return
    novo = dict(atual)
    for k in _CAMPOS_SOMA_XML:
        try:
            novo[k] = f"{float(atual.get(k) or 0) + float(item_nf.get(k) or 0):.4f}"
        except (TypeError, ValueError):
            pass
    destino["xml_item_nf"] = novo


def _br_nfdoc_nfse(dados_nf: dict):
    """Documento fiscal da NFS-e padrão nacional: categoria SE + dados da chave de acesso (analista fiscal, 29/10/2026:
    a fatura saiu sem categoria; o Contas a Pagar preenche Ctg.NF = SE e a chave). Chave de 50 dígitos:
    município(7) + ambiente gerador(1) + tipo de inscrição(1) + CNPJ/CPF(14) + nº NFS-e(13) + AAMM(4)
    + código numérico(9) + DV(1). Sem chave de 50 dígitos: None (sem documento fiscal, como antes).
    A API não tem campo para "Sist. geração NFS-e" e ignora o nº aleatório na SE (QAS, nota 6944): esses dois o
    Contas a Pagar completa (ver pendencia_nfse; o card fica na fase, como o CT-e)."""
    chave = re.sub(r"\D", "", str(dados_nf.get("chave_acesso") or ""))
    if len(chave) != 50:
        return None
    # Com documento fiscal, o SAP põe a referência no "Nº NF" do cabeçalho, que aceita até 6 dígitos: número maior
    # dá "8B/235 Formato do nº NF externo/série está incorreto" (QAS, 02/11/2026: 10 lançou; 1455275 e 915817327
    # não). Acima de 6 dígitos a NFS-e continua sem documento fiscal, como antes.
    numero = re.sub(r"\D", "", str(dados_nf.get("numero_nf") or ""))
    if not numero or len(numero.lstrip("0")) > 6:
        log.info("NFS-e %s: número com mais de 6 dígitos; lançando sem categoria SE (como antes).", dados_nf.get("numero_nf"))
        return None
    br = {
        "BR_NFType": "SE",
        "BR_NFSNumber": str(dados_nf.get("numero_nf") or "").strip() or str(int(chave[23:36])),   # nNFSe (= nº na chave)
        "BR_NFeRandomNumber": chave[40:49],
        "IssuingType": "1",
    }
    if dados_nf.get("data_protocolo"): br["BR_NFAuthenticationDate"] = dados_nf["data_protocolo"]
    if dados_nf.get("hora_protocolo"): br["BR_NFAuthenticationTime"] = dados_nf["hora_protocolo"]
    return br


def pendencia_nfse(dados_nf: dict) -> str:
    """O que o Contas a Pagar completa à mão numa NFS-e lançada pela automação (texto para o comentário do card)."""
    n = dados_nf.get("numero_nf", "")
    chave = re.sub(r"\D", "", str(dados_nf.get("chave_acesso") or ""))
    if _br_nfdoc_nfse(dados_nf):
        return (f"• NF {n}: lançada com Ctg.NF SE. Completar em Nota fiscal > Dados NF serviço: "
                f"Sist. geração NFS-e = {chave[7]} e Nº aleatório = {chave[40:49]}.")
    if len(chave) == 50:
        return (f"• NF {n}: lançada SEM Ctg.NF SE (número com mais de 6 dígitos; o SAP não aceita pela API). "
                f"Preencher a categoria e a chave à mão: {chave}.")
    return f"• NF {n}: XML sem chave de acesso nacional; lançada sem categoria SE."


def _montar_payload_1_to_1(dados_nf: dict, card_data: dict, company_config: dict, banco_de_migos_secundario: dict = None) -> dict:
    tipo_doc = dados_nf["tipo"]
    _limpar_xml_item_nf(card_data)
    pedido_sap = card_data.get("numero_pedido_sap", "") 

    company = company_config.get("company_code")
    if not company:
        raise ValueError("Erro Crítico de Configuração: Company Code não definido.")

    business_place = company_config.get("business_place")
    if not business_place:
        raise ValueError(f"Local de Negócios não está mapeado no sistema para esta filial.")

    numero_nf_final = card_data.get("numero_nf_override") or dados_nf.get("numero_nf", "")
    tipo_op = card_data.get("tipo_operacao", "normal")
    operacoes = _operacoes_do_card(card_data)
    eh_materia_prima = "materia_prima" in operacoes
    
    texto_partes = [f"NF {numero_nf_final}"]
    card_id = card_data.get("card_id")
    if card_id and card_id != "app": 
        texto_partes.append(f"ID {card_id}")
    if tipo_op != "normal":
        texto_partes.append(tipo_op.replace("_", " ").capitalize())
        
    if "cartoes" in operacoes and card_data.get("texto"):
        texto_historico = str(card_data.get("texto")).strip()
    else:
        texto_historico = ", ".join(texto_partes)
        
    valor_str = f"{float(dados_nf.get('valor_total', 0)):.2f}"
    hoje_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    data_lancamento_hoje = f"/Date({hoje_ms})/"

    serie_raw = str(dados_nf.get('serie', '0')).strip()
    serie_limpa = serie_raw.lstrip('0')
    if not serie_limpa:  
        serie_limpa = "0"
    
    # NFS-e: só o número, como o Contas a Pagar lança à mão (levantamento_nfse: ref "73", "6815"...).
    invoice_reference = str(numero_nf_final) if tipo_doc == "NFSE" else f"{numero_nf_final}-{serie_limpa}"

    payload: dict = {
        "CompanyCode":                   company,
        "DocumentDate":                  dados_nf.get("data_emissao"), 
        "PostingDate":                   data_lancamento_hoje, 
        "SupplierInvoiceIDByInvcgParty": invoice_reference,
        "DocumentCurrency":              "BRL",
        "InvoiceGrossAmount":            valor_str,
        "SupplierPostingLineItemText":   texto_historico[:50], 
        "AccountingDocumentType":        company_config.get("doc_type", "RE"),
        "TaxIsCalculatedAutomatically":  True, 
        "BusinessPlace":                 business_place,
    }

    vencimento_retroativo = False
    data_venc_raw = dados_nf.get("data_vencimento_raw")
    if data_venc_raw:
        try:
            dt_venc = datetime.strptime(data_venc_raw[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if dt_venc.date() <= datetime.now(timezone.utc).date():
                vencimento_retroativo = True
        except Exception:
            pass

    card_data["vencimento_retroativo"] = vencimento_retroativo 

    lista_bloqueio = ["compra_site", "adiantamento", "agenciamento", "cartoes", "cte"]
    condicao_sap = card_data.get("cond_pagamento") or card_data.get("payment_terms")
    
    if condicao_sap:
        payload["PaymentTerms"] = str(condicao_sap).upper().strip()
    elif operacoes & set(lista_bloqueio):
        log.info(f"Operação {sorted(operacoes & set(lista_bloqueio))} sem condição de pagamento. Forçando 'A001'.")
        payload["PaymentTerms"] = "A001"

    subir_bloqueado = card_data.get("subir_bloqueado_pagamento") or False
    if subir_bloqueado or (operacoes & set(lista_bloqueio)):
        payload["PaymentBlockingReason"] = "A"
        
    t_pag = str(dados_nf.get("forma_pagamento", "")).strip()
    mapa_pgto = {
        "02": "C", "03": "K", "04": "K", "14": "B", "15": "B", 
        "16": "A", "17": "2", "18": "T", "90": "D",
    }
    if t_pag in mapa_pgto:
        payload["PaymentMethod"] = mapa_pgto[t_pag]

    if pedido_sap:
        cnpj_xml = str(dados_nf.get("cnpj_emitente", "")).strip()
        cnpj_sap = str(card_data.get("cnpj_fornecedor_sap", "")).strip() 
        
        if cnpj_xml != cnpj_sap:
            raise ValueError(f"Bloqueio Antifraude: O CNPJ emissor ({cnpj_xml}) diverge do SAP ({cnpj_sap}).")
        
        itens_nf = dados_nf.get("itens", [])
        itens_po = card_data.get("itens_po", []) 

        if not itens_nf:
            raise ValueError("O XML da Nota Fiscal não possui linhas de itens válidas.")

        agrupamento_po = {}
        migo_ultimo_usado_por_item = {}
        qtd_consumida_migo_por_item = {}
        po_disponiveis = copy.deepcopy(itens_po) 
        pedidos_do_card = {str(p.get("po_number", "")).lstrip("0") for p in itens_po}
        nf_num_ref, nf_serie_ref = dados_nf.get("numero_nf"), dados_nf.get("serie")
        # Recebimento lançado com a referência desta NF, "juntando linhas": várias linhas da NF do
        # mesmo material num único item do Pedido (NF 23759 / Pedido 4569933302: 4 linhas da NF em
        # 2 itens recebidos). Quantidade recebida com a referência da NF, por item do Pedido; o item
        # continua disponível para as próximas linhas da NF enquanto houver esse saldo.
        saldo_ref_nf = {}
        for _p in po_disponiveis:
            _q = sum(float(m.get("qty", 0) or 0) for m in (_p.get("lista_migos_disponiveis") or [])
                     if _referencia_migo_bate_com_nf(m.get("ref", ""), nf_num_ref, nf_serie_ref))
            if _q > 0.0001:
                saldo_ref_nf[f"{str(_p.get('po_number', '')).lstrip('0')}-{_p.get('po_item')}"] = _q
        itens_ja_usados = set()

        # Fila em vez de for: uma linha da NF pode ser dividida entre dois itens do Pedido quando o
        # recebimento com a referência da NF dividiu a quantidade de outro jeito (ver divisão abaixo).
        fila_nf = [(i, it) for i, it in enumerate(itens_nf)]
        while fila_nf:
            idx, item_nf = fila_nf.pop(0)
            nf_qty = float(item_nf.get("quantidade", 1.0))
            nf_val_bruto = float(item_nf.get("valor", 0.0))
            nf_ncm = str(item_nf.get("ncm", "")).replace(".", "").strip()
            
            best_match = None
            best_score = -1
            qty_final_matched = 0.0
            motivos_recusa = []
            
            for po_item in po_disponiveis:
                ncm_po = po_item.get("ncm_cadastro", "")
                
                if nf_ncm and ncm_po:
                    if eh_materia_prima:
                        if nf_ncm != ncm_po: continue
                    else:
                        if nf_ncm[:4] != ncm_po[:4]: continue

                score = 0
                match_qty = 0.0
                bruto_sap = po_item.get("bruto_sap_esperado", 0.0)
                
                diferenca_centavos = abs(nf_val_bruto - bruto_sap)
                if bruto_sap > 0 and nf_val_bruto > 0 and diferenca_centavos <= 0.10:
                    score += 50
                    match_qty = float(po_item.get("qtd_pedida", 0.0))
                else:
                    try:
                        qty_convertida = converter_quantidade(nf_qty, item_nf.get("unidade", "UN"), po_item["unit"])
                        preco_unit_nf = nf_val_bruto / qty_convertida if qty_convertida > 0 else 0
                        
                        qtd_po = float(po_item.get("qtd_pedida", 1.0))
                        if qtd_po <= 0: qtd_po = 1.0 
                        preco_unit_po_bruto = bruto_sap / qtd_po
                        
                        if preco_unit_po_bruto > 0 and abs(preco_unit_nf - preco_unit_po_bruto) / preco_unit_po_bruto <= 0.01:
                            if qty_convertida > (qtd_po + 0.05):
                                motivos_recusa.append(f"A quantidade da nota ({qty_convertida} {po_item['unit']}) é maior que o saldo da linha ({qtd_po} {po_item['unit']}). Forçando Rateio 1:N.")
                            else:
                                score += 40
                                match_qty = qty_convertida 
                        else:
                            motivos_recusa.append(f"Incompatibilidade de Preço: Pedido SAP (R$ {preco_unit_po_bruto:.2f}) vs Nota (R$ {preco_unit_nf:.2f}).")
                    except ValueError:
                        motivos_recusa.append(f"Unidade incompatível (Nota: {item_nf.get('unidade', 'UN')} vs SAP: {po_item['unit']}) e valores brutos não batem exato.")
                    
                if score == 0:
                    continue

                # Prioridade para o item do Pedido indicado no próprio XML (xPed/nItemPed).
                # Só soma se o item já passou por NCM e valor/preço acima; sem indicação no XML,
                # nada muda.
                # Exceção (NF 27292, 13/10): item indicado no XML que exige recebimento e não tem NENHUM não
                # ganha a prioridade -- a Logística recebeu a linha em outro item do mesmo material/preço
                # (pedido 4508175109: NF diz item 50, recebimento 5053238195 lançado no item 70). Assim vence
                # o item recebido com a referência desta NF; sem outro candidato, o erro de recebimento continua.
                if _item_indicado_no_xml(item_nf, po_item, pedidos_do_card) and not (
                        po_item.get("gr_based_iv") and not po_item.get("lista_migos_disponiveis")):
                    score += 100
                # Sem indicação no XML (ou empatado): prefere o item cujo recebimento foi lançado
                # com a referência desta própria NF.
                _chave_ref = f"{str(po_item.get('po_number', '')).lstrip('0')}-{po_item.get('po_item')}"
                if _chave_ref in saldo_ref_nf:
                    # recebido com a referência desta NF: +30 se ainda cabe esta linha, +20 se cabe só parte
                    score += 30 if saldo_ref_nf[_chave_ref] + 0.05 >= match_qty else 20
                elif _item_tem_recebimento_da_nf(po_item, nf_num_ref, nf_serie_ref):
                    score += 30
                    
                if bruto_sap > 0 and nf_val_bruto > 0:
                    if diferenca_centavos <= 0.02: score += 20
                    elif diferenca_centavos <= 0.10: score += 10

                if po_item.get("lista_migos_disponiveis"): score += 15
                if nf_ncm == ncm_po: score += 5

                # Desempate final: data de remessa mais antiga; persistindo, o primeiro item (como antes).
                if score > best_score or (
                    score == best_score and best_match is not None
                    and _chave_data_remessa(po_item) < _chave_data_remessa(best_match)
                ):
                    best_score = score
                    best_match = po_item
                    qty_final_matched = match_qty

            if not best_match:
                if motivos_recusa:
                    erros_unicos = list(set(motivos_recusa))
                    detalhe_erro = " e ".join(erros_unicos)
                    raise ValueError(f"MATCH_FAIL: Inconsistência no item {idx+1} da Nota Fiscal! Motivo: {detalhe_erro}. (Pedido: {pedido_sap})")
                else:
                    raise ValueError(f"MATCH_FAIL: Inconsistência no item {idx+1}! Não há itens compatíveis com este NCM ou Valor disponíveis no Pedido {pedido_sap}.")

            # Recebimento com a referência desta NF menor que a linha da NF, e outro item do Pedido também
            # recebido com a referência da NF: fatura neste item só o que foi recebido nele e manda o resto
            # da linha para os próximos itens. Ex.: NF 23759 tem 160+160+24+42 e o recebimento lançou 292 no
            # item 40 e 94 no item 160 -- nenhuma combinação de linhas inteiras fecha esses números.
            _ch_best = f"{str(best_match.get('po_number', '')).lstrip('0')}-{best_match.get('po_item')}"
            _saldo_best = saldo_ref_nf.get(_ch_best, 0.0)
            if (0.05 < _saldo_best < qty_final_matched - 0.05
                    and any(f"{str(p.get('po_number', '')).lstrip('0')}-{p.get('po_item')}" in saldo_ref_nf
                            for p in po_disponiveis if p is not best_match)):
                _frac = _saldo_best / qty_final_matched
                _q_nf = float(item_nf.get("quantidade", 0) or 0)
                _v_nf = float(item_nf.get("valor", 0) or 0)
                _resto = dict(item_nf, quantidade=f"{_q_nf * (1 - _frac):.4f}", valor=f"{_v_nf * (1 - _frac):.2f}")
                item_nf = dict(item_nf, quantidade=f"{_q_nf * _frac:.4f}", valor=f"{_v_nf - float(_resto['valor']):.2f}")
                for _k in ("vICMS", "vIPI", "vPIS", "vCOFINS"):
                    try:
                        _t = float(_resto.get(_k) or 0)
                        _resto[_k] = f"{_t * (1 - _frac):.2f}"
                        item_nf[_k] = f"{_t - float(_resto[_k]):.2f}"
                    except (TypeError, ValueError):
                        pass
                fila_nf.insert(0, (idx, _resto))
                log.info(f"Linha {idx + 1} da NF dividida: {_saldo_best:g} no Item {best_match.get('po_item')} (recebido com a "
                         f"referência da NF) e {qty_final_matched - _saldo_best:g} para o próximo item recebido.")
                qty_final_matched = _saldo_best

            match_po = best_match
            _acumular_xml_item(match_po, item_nf)
            
            for orig_item in card_data.get("itens_po", []):
                if orig_item["po_number"] == match_po["po_number"] and orig_item["po_item"] == match_po["po_item"]:
                    _acumular_xml_item(orig_item, item_nf)
                    break
            
            po_number  = match_po["po_number"].lstrip("0")
            po_item_id = match_po["po_item"]

            gr_iv = match_po.get("gr_based_iv", False)
            # Mesmo com a flag RevFatEM desmarcada, se for materia-prima com utilizacao 1,
            # ainda buscamos e consumimos um MIGO real (se existir) para confirmar que o
            # recebimento fisico ocorreu e a quantidade bate -- e' so um gate de validacao
            # interna: como a flag esta' desmarcada, o SAP espera fatura PO-based (sem
            # referencia a GR), entao r_doc/r_year/r_item nunca sao enviados ao SAP nesse
            # caso (ver "gr_based_iv": gr_iv abaixo, que preserva o valor real da flag).
            precisa_migo = (gr_iv or (eh_materia_prima and match_po.get("material_usage") == "1")) \
                and not card_data.get("validacao_sem_recebimento")
            r_doc = ""
            r_year = ""
            r_item = ""
            
            if precisa_migo:
                chave_migo_banco = f"{po_number}-{po_item_id}"
                
                if banco_de_migos_secundario is not None and chave_migo_banco in banco_de_migos_secundario:
                    migos_disponiveis = banco_de_migos_secundario[chave_migo_banco]
                else:
                    migos_disponiveis = match_po.get("lista_migos_disponiveis", [])
                    
                reuso_ref_nf = (
                    chave_migo_banco in itens_ja_usados and chave_migo_banco in migo_ultimo_usado_por_item
                    and not any(_referencia_migo_bate_com_nf(m.get("ref", ""), nf_num_ref, nf_serie_ref)
                                for m in (migos_disponiveis or []))
                )
                if reuso_ref_nf:
                    _ult = migo_ultimo_usado_por_item[chave_migo_banco]
                    r_doc, r_year, r_item = _ult.get("doc", ""), _ult.get("year", ""), _ult.get("item", "")
                    log.info(f"Mesmo recebimento {r_doc}/{r_item} (referência da NF) usado para mais uma linha da NF "
                             f"no Item {po_item_id} do Pedido {po_number} (recebimento juntou linhas da NF).")
                else:
                    r_doc, r_year, r_item = _selecionar_referencia_migo(
                        migos_disponiveis, po_number, po_item_id, ("materia_prima" if eh_materia_prima else tipo_op),
                        migo_ultimo_usado_por_item, chave_migo_banco,
                        nf_numero=dados_nf.get("numero_nf"), nf_serie=dados_nf.get("serie"),
                    )
                _validar_saldo_recebimento(
                    qtd_consumida_migo_por_item, chave_migo_banco, qty_final_matched,
                    match_po.get("qtd_recebida_total", 0.0), po_number, po_item_id
                )
            
            tax_code = match_po.get("tax_code")
            if not tax_code:
                raise ValueError(f"O item {po_item_id} do Pedido {po_number} está sem 'Código de Imposto' (Tax Code).")

            sap_unit_code = match_po["unit"] if match_po["unit"] else _converter_unidade(item_nf.get("unidade", "UN"))

            impostos_item = match_po.get("impostos_sap") or {}
            bruto_esperado_item = float(match_po.get("bruto_sap_esperado", 0.0))
            qtd_pedida_item = float(match_po.get("qtd_pedida", 0.0))

            if impostos_item and bruto_esperado_item > 0 and qtd_pedida_item > 0:
                # Liquido correto: bruto do item (PMP0+ZD01+ZO01) menos ICMS+PIS+COFINS
                # (condicoes nao-estatisticas do proprio Pedido). O IPI (ZIPI) NAO entra
                # aqui: ele nunca fez parte do bruto (vProd na nota ja exclui o IPI, que e
                # somado por fora para chegar no vNF) e e tratado automaticamente pelo SAP
                # na hora do lancamento. Ver fase1_saldo_zero para a analise que validou isso.
                deducao_impostos = (
                    float(impostos_item.get("ZICM", 0.0))
                    + float(impostos_item.get("ZPIS", 0.0))
                    + float(impostos_item.get("ZCOF", 0.0))
                )
                preco_unitario_real = (bruto_esperado_item + deducao_impostos) / qtd_pedida_item
            else:
                # Fallback: sem dados de precificacao (YY1_API_PO_Pricing) para o item,
                # usa o NetPriceAmount do Pedido como antes.
                preco_unitario_real = float(match_po["preco_unit"]) / float(match_po.get("unid_preco", 1.0))

            valor_liquido_payload = preco_unitario_real * qty_final_matched
            
            chave_agrupamento = f"{po_number}-{po_item_id}-{r_doc}-{r_item}"
            
            if chave_agrupamento in agrupamento_po:
                agrupamento_po[chave_agrupamento]["SupplierInvoiceItemAmount"] += valor_liquido_payload
                agrupamento_po[chave_agrupamento]["QuantityInPurchaseOrderUnit"] += qty_final_matched
            else:
                agrupamento_po[chave_agrupamento] = {
                    "PurchaseOrder":               po_number,
                    "PurchaseOrderItem":           po_item_id,
                    "DocumentCurrency":            "BRL",
                    "SupplierInvoiceItemAmount":   valor_liquido_payload,
                    "TaxCode":                     tax_code,
                    "QuantityInPurchaseOrderUnit": qty_final_matched,
                    "PurchaseOrderQuantityUnit":   sap_unit_code,
                    "PurchaseOrderPriceUnit":      match_po.get("po_price_unit", ""),
                    "ReferenceDocument":           r_doc,
                    "ReferenceDocumentFiscalYear": str(r_year),
                    "ReferenceDocumentItem":       str(r_item),
                    "gr_based_iv":                 gr_iv,
                    "fretes_planejados":           match_po.get("fretes_planejados", []) 
                }
            
            _chave_usada = f"{po_number}-{po_item_id}"
            itens_ja_usados.add(_chave_usada)
            if _chave_usada in saldo_ref_nf:
                saldo_ref_nf[_chave_usada] -= qty_final_matched
                if saldo_ref_nf[_chave_usada] <= 0.05:
                    del saldo_ref_nf[_chave_usada]
                    po_disponiveis.remove(match_po)
            else:
                po_disponiveis.remove(match_po)

        linhas = []
        for i, (k, d) in enumerate(agrupamento_po.items()):
            item_payload = {
                "SupplierInvoiceItem":         str(len(linhas) + 1),
                "PurchaseOrder":               d["PurchaseOrder"],
                "PurchaseOrderItem":           d["PurchaseOrderItem"],
                "DocumentCurrency":            "BRL",
                "SupplierInvoiceItemAmount":   f"{d['SupplierInvoiceItemAmount']:.2f}",
                "TaxCode":                     d["TaxCode"],
                "QuantityInPurchaseOrderUnit": f"{d['QuantityInPurchaseOrderUnit']:.4f}",
                "PurchaseOrderQuantityUnit":   d["PurchaseOrderQuantityUnit"]
            }
            
            if d.get("PurchaseOrderPriceUnit"):
                item_payload["PurchaseOrderPriceUnit"] = d["PurchaseOrderPriceUnit"]
            
            if d.get("gr_based_iv") and d.get("ReferenceDocument") and str(d.get("ReferenceDocument")) != "None":
                item_payload["ReferenceDocument"] = d["ReferenceDocument"]
                item_payload["ReferenceDocumentFiscalYear"] = d["ReferenceDocumentFiscalYear"]
                item_payload["ReferenceDocumentItem"] = str(d["ReferenceDocumentItem"]).zfill(4)
                
            linhas.append(item_payload)
            
            for frete in d.get("fretes_planejados", []):
                if abs(frete['amount']) <= 0.01: 
                    continue 
                
                frete_payload = {
                    "SupplierInvoiceItem":           str(len(linhas) + 1),
                    "PurchaseOrder":                 d["PurchaseOrder"],
                    "PurchaseOrderItem":             d["PurchaseOrderItem"],
                    "DocumentCurrency":              "BRL",
                    "SupplierInvoiceItemAmount":     f"{frete['amount']:.2f}",
                    "TaxCode":                       "LI", 
                    "QuantityInPurchaseOrderUnit":   f"{d['QuantityInPurchaseOrderUnit']:.4f}",
                    "PurchaseOrderQuantityUnit":     d["PurchaseOrderQuantityUnit"],
                    "SuplrInvcDeliveryCostCndnStep": frete["step"],
                    "SuplrInvcDeliveryCostCndnCount": frete["counter"],
                }
                
                if frete.get("supplier"): 
                    frete_payload["FreightSupplier"] = frete["supplier"]

                # Unidade de preço diferente da unidade do pedido (ex.: pedido em UN, preço por M):
                # a linha de frete planejado também precisa da unidade de preço, senão o SAP
                # recebe o campo vazio e devolve BM/034 "código ISO SPACE". Só nesse caso -- quando
                # as unidades são iguais a linha de frete continua exatamente como antes.
                upp = str(d.get("PurchaseOrderPriceUnit") or "").strip()
                if upp and upp != str(d.get("PurchaseOrderQuantityUnit") or "").strip():
                    frete_payload["PurchaseOrderPriceUnit"] = upp
                    
                linhas.append(frete_payload)
            
        payload["to_SuplrInvcItemPurOrdRef"] = linhas

    if tipo_doc in ("NFE", "CTE") and dados_nf.get("chave_acesso"):
        chave = re.sub(r"\D", "", str(dados_nf["chave_acesso"]))
        br_data = {
            "BR_NFType": "YA" if tipo_doc == "NFE" else "C7",
            "BR_NFAuthznProtocolNumber": dados_nf.get("protocolo", ""),
            "BR_NFeRandomNumber": chave[34:43] if len(chave) == 44 else dados_nf.get("numero_aleatorio", ""),
            "IssuingType": str(dados_nf.get("tp_emis", "1")),
        }
        if dados_nf.get("data_protocolo"): br_data["BR_NFAuthenticationDate"] = dados_nf.get("data_protocolo")
        if dados_nf.get("hora_protocolo"): br_data["BR_NFAuthenticationTime"] = dados_nf.get("hora_protocolo")
        payload["to_BR_SupplierInvoiceNFDocument"] = br_data
    elif tipo_doc == "NFSE":
        br_nfse = _br_nfdoc_nfse(dados_nf)
        if br_nfse:
            payload["to_BR_SupplierInvoiceNFDocument"] = br_nfse

    return payload


TOLERANCIA_RATEIO_VALOR = 0.02  # R$


def _subconjunto_por_valor(item_nf: dict, itens_po: list, nf_ncm: str, nf_val_bruto: float, dados_nf: dict):
    """
    Último recurso (só depois de 1:1 e 1:N falharem): a linha da NF vem numa unidade "de pacote"
    (ex.: 1 UN = pacote com 500 sacos) e o Pedido em unidades avulsas, às vezes em mais de um item.
    Procura um conjunto de itens do Pedido, ainda inteiros em aberto, do MESMO material e MESMO NCM
    (8 dígitos), já recebidos, cujo valor bruto somado é igual ao valor da linha da NF (± R$ 0,02).
    Só aceita se a escolha não for ambígua (todos os conjuntos que batem têm o mesmo tamanho e o
    mesmo material, ou seja, são intercambiáveis); nesse caso usa o desempate de sempre.
    Devolve a lista de itens do Pedido ou None.
    """
    from itertools import combinations
    if not nf_ncm or len(nf_ncm) < 8 or nf_val_bruto <= 0:
        return None
    elegiveis = []
    for po in itens_po:
        qtd = float(po.get("qtd_pedida", 0) or 0)
        if qtd <= 0 or abs(po.get("saldo_memoria", 0) - qtd) > 0.001:
            continue                      # só item inteiro em aberto
        if str(po.get("ncm_cadastro", "")) != nf_ncm or not str(po.get("material", "")).strip():
            continue
        if float(po.get("bruto_sap_esperado", 0) or 0) <= 0:
            continue
        if po.get("gr_based_iv") and float(po.get("qtd_recebida_total", 0) or 0) < qtd - 0.05:
            continue                      # sem recebimento completo, não entra
        elegiveis.append(po)

    por_material = {}
    for po in elegiveis:
        por_material.setdefault(str(po["material"]).strip(), []).append(po)

    achados = []
    for material, grupo in por_material.items():
        if len(grupo) > 12:
            continue
        for n in range(1, len(grupo) + 1):
            for combo in combinations(grupo, n):
                soma = sum(float(p["bruto_sap_esperado"]) for p in combo)
                if abs(soma - nf_val_bruto) <= TOLERANCIA_RATEIO_VALOR:
                    achados.append((material, n, list(combo)))
    if not achados:
        return None
    if len({(m, n) for m, n, _ in achados}) > 1:
        log.info("Rateio por valor: mais de um conjunto possível no Pedido para o item da NF; não escolho.")
        return None

    pedidos_do_card = {str(p.get("po_number", "")).lstrip("0") for p in itens_po}
    def chave(p):
        return (0 if _item_indicado_no_xml(item_nf, p, pedidos_do_card) else 1,
                0 if _item_tem_recebimento_da_nf(p, dados_nf.get("numero_nf"), dados_nf.get("serie")) else 1,
                _chave_data_remessa(p), p["po_number"], p["po_item"])
    return min((sorted(c, key=chave) for _, _, c in achados), key=lambda c: [chave(p) for p in c])


# Pedido indicado no XML (xPed) que não está no campo do card (pedido do João, 27/10/2026).
# Caso real: NF 21368 (card 1448240239) -- o card tinha só o 4524012602, mas o item 1 da NF era do
# 4514141740 (xPed no XML). A automação parava em "Não há itens compatíveis" e a analista fiscal lançou à mão.
# Regra: o pedido do XML só é carregado DEPOIS que o fluxo de sempre falhou por falta de item compatível,
# e, para cada item da NF, só é olhado se o pedido do card não cobrir aquele item (ordem: card normal ->
# card rateio por valor -> XML normal -> XML rateio por valor). Só entra pedido nosso (45 + 8 dígitos),
# do mesmo fornecedor (CNPJ do XML) e da mesma empresa. Sem xPed fora do card, nada muda.
# Desligar com PEDIDO_EXTRA_XML=0.
PEDIDO_EXTRA_XML = _os.getenv("PEDIDO_EXTRA_XML", "1").strip() == "1"
_RE_PEDIDO_NOSSO = re.compile(r"45\d{8}")


def _pedidos_xml_fora_do_card(dados_nf: dict, card_data: dict) -> list:
    """Pedidos (xPed) citados nos itens do XML que não estão no card. Só números de pedido nosso."""
    no_card = {str(p.get("po_number", "")).lstrip("0") for p in card_data.get("itens_po", [])}
    no_card |= {n.lstrip("0") for n in re.findall(r"\d+", str(card_data.get("numero_pedido_sap", "") or ""))}
    achados = []
    for it in dados_nf.get("itens", []) or []:
        ped = re.sub(r"\D", "", str(it.get("pedido_xml", "") or "")).lstrip("0")
        if _RE_PEDIDO_NOSSO.fullmatch(ped) and ped not in no_card and ped not in achados:
            achados.append(ped)
    return achados


def _carregar_pedidos_extra_xml(dados_nf: dict, card_data: dict, company_config: dict) -> list:
    """Itens dos pedidos do XML fora do card, já validados (mesmo fornecedor e mesma empresa).
    Cada item vem marcado com _pedido_extra_xml=True. Qualquer problema: devolve [] (fica como hoje)."""
    itens = []
    cnpj_xml = re.sub(r"\D", "", str(dados_nf.get("cnpj_emitente", "") or ""))
    empresa = str(company_config.get("company_code", "") or "").strip()
    for ped in _pedidos_xml_fora_do_card(dados_nf, card_data):
        try:
            dados = buscar_empresa_por_po(ped)
        except Exception as e:
            log.info("Pedido %s do XML: não consegui ler no SAP (%s); ignorado.", ped, e)
            continue
        cnpj_po = re.sub(r"\D", "", str(dados.get("cnpj_fornecedor_sap", "") or ""))
        empresa_po = str(dados.get("company_code", "") or "").strip()
        if not dados.get("itens_po") or not cnpj_xml or cnpj_po != cnpj_xml or (empresa and empresa_po != empresa):
            log.info("Pedido %s do XML ignorado (fornecedor %s x XML %s, empresa %s x %s, %d itens).",
                     ped, cnpj_po, cnpj_xml, empresa_po, empresa, len(dados.get("itens_po") or []))
            continue
        for it in dados["itens_po"]:
            it["_pedido_extra_xml"] = True
            it["_condicoes_pedido_extra"] = list(dados.get("condicoes_encontradas") or [])
            if card_data.get("validacao_sem_recebimento") and it.get("gr_based_iv"):
                # Validação da Logística: mesmo tratamento dos itens do card (validacao_logistica._preparar_sem_recebimento)
                it["qtd_recebida_total"] = max(float(it.get("qtd_recebida_total", 0) or 0), float(it.get("qtd_pedida", 0) or 0))
            itens.append(it)
        log.info("Pedido %s do XML (fora do card) carregado como última opção: %d itens.", ped, len(dados["itens_po"]))
    return itens


def _banco_de_migos(card_data: dict) -> dict:
    banco = {}
    for item in list(card_data.get("itens_po", [])) + list(card_data.get("itens_po_extra", [])):
        if item.get("lista_migos_disponiveis"):
            banco[f"{item['po_number']}-{item['po_item']}"] = list(item["lista_migos_disponiveis"])
    return banco


def _montar_payload_1_to_n(dados_nf: dict, card_data: dict, company_config: dict, banco_de_migos_secundario: dict = None,
                           rateio_por_valor: bool = False) -> dict:
    tipo_doc = dados_nf["tipo"]
    _limpar_xml_item_nf(card_data)
    pedido_sap = card_data.get("numero_pedido_sap", "") 

    company = company_config.get("company_code")
    if not company:
        raise ValueError("Erro Crítico de Configuração: Company Code não definido.")

    business_place = company_config.get("business_place")
    if not business_place:
        raise ValueError(f"Local de Negócios não está mapeado no sistema para esta filial.")

    numero_nf_final = card_data.get("numero_nf_override") or dados_nf.get("numero_nf", "")
    tipo_op = card_data.get("tipo_operacao", "normal")
    operacoes = _operacoes_do_card(card_data)
    eh_materia_prima = "materia_prima" in operacoes
    
    texto_partes = [f"NF {numero_nf_final}"]
    card_id = card_data.get("card_id")
    if card_id and card_id != "app": 
        texto_partes.append(f"ID {card_id}")
    if tipo_op != "normal":
        texto_partes.append(tipo_op.replace("_", " ").capitalize())
        
    if "cartoes" in operacoes and card_data.get("texto"):
        texto_historico = str(card_data.get("texto")).strip()
    else:
        texto_historico = ", ".join(texto_partes)
        
    valor_str = f"{float(dados_nf.get('valor_total', 0)):.2f}"
    hoje_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    data_lancamento_hoje = f"/Date({hoje_ms})/"

    serie_raw = str(dados_nf.get('serie', '0')).strip()
    serie_limpa = serie_raw.lstrip('0')
    if not serie_limpa:  
        serie_limpa = "0"
    
    # NFS-e: só o número, como o Contas a Pagar lança à mão (levantamento_nfse: ref "73", "6815"...).
    invoice_reference = str(numero_nf_final) if tipo_doc == "NFSE" else f"{numero_nf_final}-{serie_limpa}"

    payload: dict = {
        "CompanyCode":                   company,
        "DocumentDate":                  dados_nf.get("data_emissao"), 
        "PostingDate":                   data_lancamento_hoje, 
        "SupplierInvoiceIDByInvcgParty": invoice_reference,
        "DocumentCurrency":              "BRL",
        "InvoiceGrossAmount":            valor_str,
        "SupplierPostingLineItemText":   texto_historico[:50], 
        "AccountingDocumentType":        company_config.get("doc_type", "RE"),
        "TaxIsCalculatedAutomatically":  True, 
        "BusinessPlace":                 business_place,
    }

    vencimento_retroativo = False
    data_venc_raw = dados_nf.get("data_vencimento_raw")
    if data_venc_raw:
        try:
            dt_venc = datetime.strptime(data_venc_raw[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if dt_venc.date() <= datetime.now(timezone.utc).date():
                vencimento_retroativo = True
        except Exception:
            pass

    card_data["vencimento_retroativo"] = vencimento_retroativo 

    lista_bloqueio = ["compra_site", "adiantamento", "agenciamento", "cartoes", "cte"]
    condicao_sap = card_data.get("cond_pagamento") or card_data.get("payment_terms")
    
    if condicao_sap:
        payload["PaymentTerms"] = str(condicao_sap).upper().strip()
    elif operacoes & set(lista_bloqueio):
        log.info(f"Operação {sorted(operacoes & set(lista_bloqueio))} sem condição de pagamento. Forçando 'A001'.")
        payload["PaymentTerms"] = "A001"

    subir_bloqueado = card_data.get("subir_bloqueado_pagamento") or False
    if subir_bloqueado or (operacoes & set(lista_bloqueio)):
        payload["PaymentBlockingReason"] = "A"
        
    t_pag = str(dados_nf.get("forma_pagamento", "")).strip()
    mapa_pgto = {
        "02": "C", "03": "K", "04": "K", "14": "B", "15": "B", 
        "16": "A", "17": "2", "18": "T", "90": "D",
    }
    if t_pag in mapa_pgto:
        payload["PaymentMethod"] = mapa_pgto[t_pag]

    if pedido_sap:
        cnpj_xml = str(dados_nf.get("cnpj_emitente", "")).strip()
        cnpj_sap = str(card_data.get("cnpj_fornecedor_sap", "")).strip() 
        
        if cnpj_xml != cnpj_sap:
            raise ValueError(f"Bloqueio Antifraude: O CNPJ emissor ({cnpj_xml}) diverge do SAP ({cnpj_sap}).")
        
        itens_nf = dados_nf.get("itens", [])
        itens_po = copy.deepcopy(card_data.get("itens_po", [])) 
        # Pedido do XML fora do card (só existe na 2ª tentativa; ver _carregar_pedidos_extra_xml)
        _chaves_card = {(p["po_number"], p["po_item"]) for p in itens_po}
        itens_po += [x for x in copy.deepcopy(card_data.get("itens_po_extra", []))
                     if (x["po_number"], x["po_item"]) not in _chaves_card]

        agrupamento_po = {}
        migo_ultimo_usado_por_item = {}
        qtd_consumida_migo_por_item = {}
        
        for po_item in itens_po:
            po_item["saldo_memoria"] = float(po_item.get("qtd_pedida", 0.0))

        for idx, item_nf in enumerate(itens_nf):
            nf_qty = float(item_nf.get("quantidade", 1.0))
            nf_val_bruto = float(item_nf.get("valor", 0.0))
            nf_ncm = str(item_nf.get("ncm", "")).replace(".", "").strip()
            preco_unit_nf_bruto = nf_val_bruto / nf_qty if nf_qty > 0 else 0
            
            candidatos_validos = []
            
            for po_item in itens_po:
                if po_item["saldo_memoria"] <= 0.001: continue
                
                ncm_po = po_item.get("ncm_cadastro", "")
                
                if nf_ncm and ncm_po:
                    if eh_materia_prima and nf_ncm != ncm_po: continue
                    elif not eh_materia_prima and nf_ncm[:4] != ncm_po[:4]: continue
                
                try:
                    qty_convertida = converter_quantidade(nf_qty, item_nf.get("unidade", "UN"), po_item["unit"])
                    preco_unit_nf_bruto_convertido = nf_val_bruto / qty_convertida if qty_convertida > 0 else 0
                except ValueError:
                    continue 
                
                qtd_po = float(po_item.get("qtd_pedida", 1.0))
                if qtd_po <= 0: qtd_po = 1.0
                preco_unit_po_bruto = po_item["bruto_sap_esperado"] / qtd_po
                
                if preco_unit_po_bruto > 0 and abs(preco_unit_nf_bruto_convertido - preco_unit_po_bruto) / preco_unit_po_bruto <= 0.01:
                    candidatos_validos.append(po_item)
            
            # Pedido do card primeiro; o pedido do XML fora do card só entra se o do card não cobrir o item.
            # Sem pedido extra (caso normal), candidatos_extra fica vazio e tudo segue como antes.
            candidatos_extra = [c for c in candidatos_validos if c.get("_pedido_extra_xml")]
            candidatos_validos = [c for c in candidatos_validos if not c.get("_pedido_extra_xml")]
            itens_po_card = [p for p in itens_po if not p.get("_pedido_extra_xml")]
            itens_po_xml = [p for p in itens_po if p.get("_pedido_extra_xml")]

            fator_valor = None
            for etapa in ("card_valor", "xml_normal", "xml_valor"):
                if candidatos_validos:
                    break
                if etapa == "xml_normal":
                    candidatos_validos = candidatos_extra
                    continue
                if not rateio_por_valor:
                    continue
                base_rateio = itens_po_card if etapa == "card_valor" else itens_po_xml
                if not base_rateio:
                    continue
                subconj = _subconjunto_por_valor(item_nf, base_rateio, nf_ncm, nf_val_bruto, dados_nf)
                if subconj and nf_qty > 0:
                    candidatos_validos = subconj
                    fator_valor = sum(float(p["qtd_pedida"]) for p in subconj) / nf_qty
                    log.info(
                        "Rateio por valor no item %d da NF (%s %s, R$ %.2f) -> itens %s do Pedido (%.4f %s no total).",
                        idx + 1, nf_qty, item_nf.get("unidade", ""), nf_val_bruto,
                        ", ".join(f"{p['po_number']}/{p['po_item']}" for p in subconj),
                        nf_qty * fator_valor, subconj[0].get("unit", ""))

            if candidatos_validos and candidatos_validos[0].get("_pedido_extra_xml"):
                log.info("Item %d da NF casado com o pedido %s, indicado no XML e fora do card.",
                         idx + 1, candidatos_validos[0]["po_number"])

            if not candidatos_validos:
                raise ValueError(f"Inconsistência no item {idx+1}! Não há itens compatíveis (NCM e Preço Unitário Bruto) com saldo em aberto no Pedido para fatiamento.")
            
            # Item indicado no XML (xPed/nItemPed) vai primeiro; os demais seguem a ordem de antes.
            pedidos_do_card_n = {str(p.get("po_number", "")).lstrip("0") for p in itens_po}
            # Item que exige recebimento e não tem NENHUM vai para o fim da fila (mesma regra do 1:1, NF 27292):
            # ele nunca pode ser faturado, então só é usado se não houver outro. Caso real: NF 38834 / pedido
            # 4500002869 (28/10/2026) -- 16.000 un recebidas nos itens 20 e 30 (MIGO 5046473157); o item 10
            # (4.000 un, sem recebimento) vinha primeiro e o lote parava em "MIGO ausente para o Item 00010".
            # Na validação da Logística (sem recebimento) nada muda.
            def _sem_recebimento(x):
                return bool(x.get("gr_based_iv") and not x.get("lista_migos_disponiveis")
                            and not card_data.get("validacao_sem_recebimento"))
            candidatos_validos.sort(key=lambda x: (
                1 if _sem_recebimento(x) else 0,
                0 if _item_indicado_no_xml(item_nf, x, pedidos_do_card_n) else 1,
                0 if _item_tem_recebimento_da_nf(x, dados_nf.get("numero_nf"), dados_nf.get("serie")) else 1,
                _chave_data_remessa(x),
                x["po_number"], x["po_item"],
            ))
            qty_nf_restante = nf_qty
            
            for candidato in candidatos_validos:
                if qty_nf_restante <= 0.001: break
                
                try:
                    if fator_valor:
                        qty_nf_restante_em_unid_po = qty_nf_restante * fator_valor
                    else:
                        qty_nf_restante_em_unid_po = converter_quantidade(qty_nf_restante, item_nf.get("unidade", "UN"), candidato["unit"])
                except ValueError:
                    raise ValueError(f"O item {idx+1} necessita conversão da unidade {item_nf.get('unidade')} para {candidato['unit']} (SAP), mas a regra não existe no Dicionário.")
                
                saldo_po_disponivel = candidato["saldo_memoria"]
                consumo_no_po = min(qty_nf_restante_em_unid_po, saldo_po_disponivel)
                
                candidato["saldo_memoria"] -= consumo_no_po
                abate_na_nf = consumo_no_po * (qty_nf_restante / qty_nf_restante_em_unid_po)
                qty_nf_restante -= abate_na_nf
                
                impostos_candidato = candidato.get("impostos_sap") or {}
                bruto_esperado_candidato = float(candidato.get("bruto_sap_esperado", 0.0))
                qtd_pedida_candidato = float(candidato.get("qtd_pedida", 0.0))

                if impostos_candidato and bruto_esperado_candidato > 0 and qtd_pedida_candidato > 0:
                    # Mesma logica do 1:1 -- ver comentario em _montar_payload_1_to_1.
                    deducao_impostos_candidato = (
                        float(impostos_candidato.get("ZICM", 0.0))
                        + float(impostos_candidato.get("ZPIS", 0.0))
                        + float(impostos_candidato.get("ZCOF", 0.0))
                    )
                    preco_unitario_liquido = (bruto_esperado_candidato + deducao_impostos_candidato) / qtd_pedida_candidato
                else:
                    preco_unitario_liquido = float(candidato["preco_unit"]) / float(candidato.get("unid_preco", 1.0))

                valor_liquido_parcela = consumo_no_po * preco_unitario_liquido
                
                # Auditoria POP: cada item do Pedido fica com a PARTE da linha da NF que recebeu (antes ficava com
                # a linha inteira e, com a linha dividida em 2 itens, cada metade do SAP era comparada com o total
                # do XML -- NF 38834, 28/10/2026). Linha inteira num item só: igual a antes. Mais de uma linha no
                # mesmo item: soma (antes ficava só a última).
                parte_nf = _parte_xml_item(item_nf, (abate_na_nf / nf_qty) if nf_qty > 0 else 1.0)
                _acumular_xml_item(candidato, parte_nf)
                
                for orig_item in list(card_data.get("itens_po", [])) + list(card_data.get("itens_po_extra", [])):
                    if orig_item["po_number"] == candidato["po_number"] and orig_item["po_item"] == candidato["po_item"]:
                        _acumular_xml_item(orig_item, parte_nf)
                        break
                
                gr_iv = candidato.get("gr_based_iv", False)
                # Mesma logica do 1:1: matéria-prima com utilizacao 1 sempre busca/consome
                # um MIGO real (mesmo com a flag desmarcada), mas so' envia ReferenceDocument
                # ao SAP quando a flag realmente exigir (gr_based_iv abaixo preserva o valor
                # real da flag, entao o payload final continua PO-based nesse caso).
                precisa_migo = (gr_iv or (eh_materia_prima and candidato.get("material_usage") == "1")) \
                    and not card_data.get("validacao_sem_recebimento")
                r_doc = ""
                r_year = ""
                r_item = ""

                if precisa_migo:
                    chave_migo_banco = f"{candidato['po_number']}-{candidato['po_item']}"
                    
                    if banco_de_migos_secundario is not None and chave_migo_banco in banco_de_migos_secundario:
                        migos_disponiveis = banco_de_migos_secundario[chave_migo_banco]
                    else:
                        migos_disponiveis = candidato.get("lista_migos_disponiveis", [])
                        
                    r_doc, r_year, r_item = _selecionar_referencia_migo(
                        migos_disponiveis, candidato['po_number'], candidato['po_item'], ("materia_prima" if eh_materia_prima else tipo_op),
                        migo_ultimo_usado_por_item, chave_migo_banco,
                        nf_numero=dados_nf.get("numero_nf"), nf_serie=dados_nf.get("serie"),
                    )
                    _validar_saldo_recebimento(
                        qtd_consumida_migo_por_item, chave_migo_banco, consumo_no_po,
                        candidato.get("qtd_recebida_total", 0.0), candidato['po_number'], candidato['po_item']
                    )
                
                chave_agrupamento = f"{candidato['po_number']}-{candidato['po_item']}-{r_doc}-{r_item}"
                if chave_agrupamento in agrupamento_po:
                    agrupamento_po[chave_agrupamento]["SupplierInvoiceItemAmount"] += valor_liquido_parcela
                    agrupamento_po[chave_agrupamento]["QuantityInPurchaseOrderUnit"] += consumo_no_po
                else:
                    agrupamento_po[chave_agrupamento] = {
                        "PurchaseOrder":               candidato["po_number"],
                        "PurchaseOrderItem":           candidato["po_item"],
                        "SupplierInvoiceItemAmount":   valor_liquido_parcela,
                        "TaxCode":                     candidato["tax_code"],
                        "QuantityInPurchaseOrderUnit": consumo_no_po,
                        "PurchaseOrderQuantityUnit":   candidato["unit"],
                        "PurchaseOrderPriceUnit":      candidato.get("po_price_unit", ""),
                        "ReferenceDocument":           r_doc,
                        "ReferenceDocumentFiscalYear": str(r_year),
                        "ReferenceDocumentItem":       str(r_item),
                        "gr_based_iv":                 gr_iv,
                        "fretes_planejados":           candidato.get("fretes_planejados", []) 
                    }
                    
            if qty_nf_restante > 0.01:
                raise ValueError(f"Falta de Saldo no Item {idx+1} da NF! Os itens do pedido ({pedido_sap}) não têm quantidade suficiente para absorver os {nf_qty} {item_nf.get('unidade')} faturados.")

        linhas = []
        for i, (k, d) in enumerate(agrupamento_po.items()):
            item_payload = {
                "SupplierInvoiceItem":         str(len(linhas) + 1),
                "PurchaseOrder":               d["PurchaseOrder"],
                "PurchaseOrderItem":           d["PurchaseOrderItem"],
                "DocumentCurrency":            "BRL",
                "SupplierInvoiceItemAmount":   f"{d['SupplierInvoiceItemAmount']:.2f}",
                "TaxCode":                     d["TaxCode"],
                "QuantityInPurchaseOrderUnit": f"{d['QuantityInPurchaseOrderUnit']:.4f}",
                "PurchaseOrderQuantityUnit":   d["PurchaseOrderQuantityUnit"]
            }
            
            if d.get("PurchaseOrderPriceUnit"):
                item_payload["PurchaseOrderPriceUnit"] = d["PurchaseOrderPriceUnit"]
            
            if d.get("gr_based_iv") and d.get("ReferenceDocument") and str(d.get("ReferenceDocument")) != "None":
                item_payload["ReferenceDocument"] = d["ReferenceDocument"]
                item_payload["ReferenceDocumentFiscalYear"] = d["ReferenceDocumentFiscalYear"]
                item_payload["ReferenceDocumentItem"] = str(d["ReferenceDocumentItem"]).zfill(4)
                
            linhas.append(item_payload)
            
            for frete in d.get("fretes_planejados", []):
                if abs(frete['amount']) <= 0.01: 
                    continue 
                
                frete_payload = {
                    "SupplierInvoiceItem":           str(len(linhas) + 1),
                    "PurchaseOrder":                 d["PurchaseOrder"],
                    "PurchaseOrderItem":             d["PurchaseOrderItem"],
                    "DocumentCurrency":              "BRL",
                    "SupplierInvoiceItemAmount":     f"{frete['amount']:.2f}",
                    "TaxCode":                       "LI", 
                    "QuantityInPurchaseOrderUnit":   f"{d['QuantityInPurchaseOrderUnit']:.4f}",
                    "PurchaseOrderQuantityUnit":     d["PurchaseOrderQuantityUnit"],
                    "SuplrInvcDeliveryCostCndnStep": frete["step"],
                    "SuplrInvcDeliveryCostCndnCount": frete["counter"],
                }
                
                if frete.get("supplier"): 
                    frete_payload["FreightSupplier"] = frete["supplier"]

                # Unidade de preço diferente da unidade do pedido (ex.: pedido em UN, preço por M):
                # a linha de frete planejado também precisa da unidade de preço, senão o SAP
                # recebe o campo vazio e devolve BM/034 "código ISO SPACE". Só nesse caso -- quando
                # as unidades são iguais a linha de frete continua exatamente como antes.
                upp = str(d.get("PurchaseOrderPriceUnit") or "").strip()
                if upp and upp != str(d.get("PurchaseOrderQuantityUnit") or "").strip():
                    frete_payload["PurchaseOrderPriceUnit"] = upp
                    
                linhas.append(frete_payload)
            
        payload["to_SuplrInvcItemPurOrdRef"] = linhas

    if tipo_doc in ("NFE", "CTE") and dados_nf.get("chave_acesso"):
        chave = re.sub(r"\D", "", str(dados_nf["chave_acesso"]))
        br_data = {
            "BR_NFType": "YA" if tipo_doc == "NFE" else "C7",
            "BR_NFAuthznProtocolNumber": dados_nf.get("protocolo", ""),
            "BR_NFeRandomNumber": chave[34:43] if len(chave) == 44 else dados_nf.get("numero_aleatorio", ""),
            "IssuingType": str(dados_nf.get("tp_emis", "1")),
        }
        if dados_nf.get("data_protocolo"): br_data["BR_NFAuthenticationDate"] = dados_nf.get("data_protocolo")
        if dados_nf.get("hora_protocolo"): br_data["BR_NFAuthenticationTime"] = dados_nf.get("hora_protocolo")
        payload["to_BR_SupplierInvoiceNFDocument"] = br_data
    elif tipo_doc == "NFSE":
        br_nfse = _br_nfdoc_nfse(dados_nf)
        if br_nfse:
            payload["to_BR_SupplierInvoiceNFDocument"] = br_nfse

    return payload

_ORIENTACAO_8B246 = (
    " || Orientação: o recebimento (MIGO) deste item já possui Nota Fiscal vinculada no SAP "
    "(msg 8B/246 -- o SAP não permite mais de uma NF por entrada de mercadorias). Verifique se "
    "há fatura estacionada, rascunho ou estornada para esta NF; se necessário, estorne o "
    "recebimento (102), relance o MIGO (101) e mova o card novamente."
)


def _log_diagnostico_erro_sap(resp: requests.Response, erro: dict = None):
    """
    Diagnóstico enxuto de erros do SAP -- SOMENTE LOG, não altera a mensagem levantada
    (self-healing de MIGO, fallback MATCH_FAIL e roteamento de área no main.py/dashboard.py
    dependem do texto exato do ValueError).

    Registra, numa linha só, o código da mensagem SAP (ex.: 8B/246 -- é por ele que se acha o
    texto extenso na documentação) e o transactionid/timestamp (pedidos pelo suporte SAP e pelo
    Gateway Error Log). Detalhes só entram se trouxerem algo diferente da mensagem principal.

    Obs.: a busca automática do texto extenso (longtext_url) foi removida porque o usuário de
    comunicação não tem autorização no serviço /IWBEP/MESSAGE_TEXT (retorna sempre 403).
    Qualquer falha aqui é engolida para nunca mudar o comportamento do lançamento.
    """
    try:
        if erro is None:
            try:
                erro = resp.json()
            except Exception:
                erro = {}
        err = erro.get("error", {}) if isinstance(erro, dict) else {}
        inner = err.get("innererror", {}) or {}
        codigo = err.get("code", "")
        msg_principal = (err.get("message", {}) or {}).get("value", "") if isinstance(err.get("message"), dict) else ""

        extras = []
        for d in inner.get("errordetails", []) or []:
            c, m = d.get("code", ""), d.get("message", "")
            if c != codigo or m != msg_principal:
                extras.append(f"{c}: {m}")

        linha = (f"Diagnóstico SAP | HTTP {resp.status_code} | código: {codigo or '-'} | "
                 f"transactionid: {inner.get('transactionid', '-')} | timestamp: {inner.get('timestamp', '-')}")
        if extras:
            linha += " | outros detalhes: " + " / ".join(extras)
        log.error(linha)
    except Exception as e_diag:
        log.warning("Diagnóstico SAP | falha ao montar diagnóstico: %s", e_diag)


def _tratar_erro_sap(resp: requests.Response):
    try:
        erro = resp.json()
        msg_principal = erro.get("error", {}).get("message", {}).get("value", "")
        
        detalhes = []
        for d in erro.get("error", {}).get("innererror", {}).get("errordetails", []):
            msg = d.get("message", "")
            if msg and msg not in detalhes:
                detalhes.append(msg)
        
        msg_final = msg_principal
        if detalhes:
            msg_final += " | Detalhes: " + " / ".join(detalhes)
            
        log.error("Erro SAP %s: %s", resp.status_code, msg_final)
        _log_diagnostico_erro_sap(resp, erro)
        raise ValueError(msg_final)
        
    except ValueError:
        raise  
    except Exception:
        texto_puro = resp.text[:200]
        log.error("Erro SAP %s (Texto puro): %s", resp.status_code, texto_puro)
        _log_diagnostico_erro_sap(resp)
        raise ValueError(f"O SAP recusou o lançamento (Erro HTTP {resp.status_code}).")

def _descrever_nf_duplicada(session: requests.Session, payload: dict, texto_sap: str):
    """Só roda quando o SAP já recusou por NF repetida ("Número NF ... p/forn.X existe"): procura a fatura que
    já existe e devolve uma mensagem que diz qual é. Qualquer problema na consulta -> None (fica a msg do SAP)."""
    try:
        m = re.search(r"p/\s*forn\.?\s*(\d+)", texto_sap, re.IGNORECASE)
        ref = str(payload.get("SupplierInvoiceIDByInvcgParty") or "").strip()
        if not m or not ref:
            return None
        fornecedor = m.group(1)
        r = session.get(f"{BASE}/A_SupplierInvoice", params={
            "$filter": f"SupplierInvoiceIDByInvcgParty eq '{ref}' and InvoicingParty eq '{fornecedor}'",
            "$select": "SupplierInvoice,FiscalYear,PostingDate,InvoiceGrossAmount,SupplierInvoiceOrigin,IsReversed,IsReversal",
            "$format": "json"}, headers={**_auth_headers(), "Accept": "application/json"}, **_auth_kwargs(), timeout=30)
        r.raise_for_status()
        achadas = [f for f in r.json().get("d", {}).get("results", []) if not f.get("IsReversed") and not f.get("IsReversal")]
        if not achadas:
            return None
        f = achadas[0]
        dt = re.search(r"\d+", str(f.get("PostingDate") or ""))
        quando = datetime.fromtimestamp(int(dt.group()) / 1000, tz=timezone.utc).strftime("%d/%m/%Y") if dt else "?"
        origem = "pela automação" if f.get("SupplierInvoiceOrigin") == "G" else "manualmente no SAP"
        t = re.sub(r"\s+", " ", texto_sap).strip()
        sap_curto = t[:t.lower().find("existe") + 6] if "existe" in t.lower() else t[:120]
        return (f"NF {ref} já existe no SAP: fatura {f['SupplierInvoice']}/{f['FiscalYear']}, lançada em {quando} "
                f"{origem} (R$ {float(f.get('InvoiceGrossAmount') or 0):.2f}). Nada a lançar; conferir se este card "
                f"está duplicado. (SAP: {sap_curto})")
    except Exception as e:
        log.warning("NF duplicada: não consegui localizar a fatura existente (%s)", e)
        return None


def criar_fatura(payload: dict, max_attempts: int = 6) -> dict:
    session = requests.Session()
    headers = {**_auth_headers(), "Content-Type": "application/json", "Accept": "application/json", "x-csrf-token": _fetch_csrf_token(session)}
    
    last_ajuste_aplicado = 0.0
    last_saldo_diff = 0.0
    
    for attempt in range(1, max_attempts + 1):
        resp = session.post(f"{BASE}/A_SupplierInvoice", json=payload, headers=headers, **_auth_kwargs(), timeout=300)
        
        if resp.ok:
            data = resp.json().get("d", {})
            log.info("✓ Fatura criada — doc %s / ano %s (Tentativa %d de convergência)", data.get("SupplierInvoice"), data.get("FiscalYear"), attempt)
            data["attempts_used"] = attempt 
            return data
            
        try:
            erro_json = resp.json()
            msg_principal = erro_json.get("error", {}).get("message", {}).get("value", "")
            detalhes = [d.get("message", "") for d in erro_json.get("error", {}).get("innererror", {}).get("errordetails", []) if d.get("message")]
            texto_erro_completo = msg_principal + " " + " ".join(detalhes)
            
            if "existe" in texto_erro_completo.lower() and not "criada para entrada" in texto_erro_completo.lower():
                msg_dup = _descrever_nf_duplicada(session, payload, texto_erro_completo)
                if msg_dup:
                    log.error("Erro SAP %s: %s", resp.status_code, msg_dup)
                    raise ValueError(msg_dup)
                _tratar_erro_sap(resp)
            
            match = re.search(r"d[é\w]bitos:\s*([\d\.,]+)\s*cr[é\w]ditos:\s*([\d\.,]+)", texto_erro_completo, re.IGNORECASE)
            
            if match:
                debitos = float(match.group(1).replace(",", ""))
                creditos = float(match.group(2).replace(",", ""))
                saldo_diff = creditos - debitos 
                
                if 0 < abs(saldo_diff) <= 10.00:
                    if attempt >= max_attempts:
                        log.warning(f"Limite de auto-correção atingido após {max_attempts} tentativas. Última diferença: R$ {saldo_diff:.2f}")
                        _tratar_erro_sap(resp)
                        
                    items = payload.get("to_SuplrInvcItemPurOrdRef", [])
                    if items:
                        itens_mercantis = [i for i in items if not i.get("SuplrInvcDeliveryCostCndnType")]
                        if not itens_mercantis: itens_mercantis = items
                        
                        maior_item = max(itens_mercantis, key=lambda x: float(x["SupplierInvoiceItemAmount"]))
                        valor_atual = float(maior_item["SupplierInvoiceItemAmount"])
                        
                        if attempt == 1:
                            ajuste_base = saldo_diff
                        else:
                            shift_total = last_saldo_diff - saldo_diff 
                            shift_base = last_ajuste_aplicado
                            
                            if shift_base != 0 and shift_total != 0:
                                tax_multiplier = shift_total / shift_base
                                ajuste_base = saldo_diff / tax_multiplier
                            else:
                                ajuste_base = saldo_diff
                        
                        novo_valor = valor_atual + ajuste_base
                        maior_item["SupplierInvoiceItemAmount"] = f"{novo_valor:.2f}"
                        
                        log.warning(f"Auto-correção (Tentativa {attempt}): Diff Total R$ {saldo_diff:.2f} | Aplicando ajuste na base de R$ {ajuste_base:.2f}")
                        
                        last_ajuste_aplicado = ajuste_base
                        last_saldo_diff = saldo_diff
                        continue 
        except ValueError:
            raise
        except Exception:
            pass 
            
        _tratar_erro_sap(resp)

    raise ValueError("Falha sistêmica ao criar fatura após auto-correções.")

def registrar_fatura(supplier_invoice: str, fiscal_year: str) -> bool:
    session = requests.Session()
    headers = {**_auth_headers(), "Content-Type": "application/json", "Accept": "application/json", "x-csrf-token": _fetch_csrf_token(session)}
    url = f"{BASE}/A_SupplierInvoice(SupplierInvoice='{supplier_invoice}',FiscalYear='{fiscal_year}')/PostInvoice"
    
    resp = session.post(url, json={}, headers=headers, **_auth_kwargs(), timeout=300)
    if resp.status_code == 404: return True
    if not resp.ok:
        try:
            _tratar_erro_sap(resp)
        except Exception as e:
            log.warning("Falha ao registrar fatura: %s", e)
        return False
    return resp.json().get("d", {}).get("Success", False)

def _calcular_risk_score(card_data: dict, dados_nf: dict, attempts: int, usou_1_to_n: bool) -> int:
    score = 0
    
    ajustes = max(0, attempts - 1)
    score += (ajustes * 10)
    
    itens_po = card_data.get("itens_po", [])
    mapa_pos = {}
    for item in itens_po:
        po_num = item.get("po_number")
        mapa_pos[po_num] = mapa_pos.get(po_num, 0) + 1
        
    if len(mapa_pos) >= 2:
        score += 10
        
    for po_num, qtd_itens in mapa_pos.items():
        if qtd_itens > 20: score += 20
        elif qtd_itens > 10: score += 10
            
    valor_nf = float(dados_nf.get("valor_total", 0.0))
    if valor_nf > 100000: score += 20
    elif valor_nf > 10000: score += 10
        
    if usou_1_to_n: score += 20
        
    return min(score, 100)

# Utilização obrigatória em pedido de PROJETO, por centro (regra do Fiscal, analista fiscal):
#   - 09/10/2026, depois da NF 555333 (pedido 4586617717, projeto da Matriz com Utilização 4: lançada, reparcelada e
#     estornada 2x): "projeto só pode ter Utilização 3";
#   - 14/10/2026, corrigido: "projeto Matriz tem DIFAL, então é utilização 2. Projeto filiais é utilização 3";
#   - 26/10/2026: frete (CT-e / etiqueta FRETE) não entra na regra, só nota (ver lancar_fatura_completo).
# Projeto = item com classificação contábil A (imobilizado) e centro de custo com "PRJ" no código
# (ex.: 01PRJ040, 12PRJ044). O centro é o do item do pedido (o mesmo da NF, garantido pelo Bloqueio de Filial).
# PROJETO_UTILIZACAO_POR_CENTRO = "centro:utilização" separados por vírgula; mais de uma utilização aceita com "|"
# (ex.: "1001:2|3"). Centro fora da lista não é bloqueado. Desligar tudo com BLOQUEIO_PROJETO_UTILIZACAO=0.
BLOQUEIO_PROJETO_UTILIZACAO = _os.getenv("BLOQUEIO_PROJETO_UTILIZACAO", "1").strip() == "1"
PROJETO_CLASSIFICACOES = {c.strip().upper() for c in _os.getenv("PROJETO_CLASSIFICACOES", "A").split(",") if c.strip()}
PROJETO_CENTRO_CUSTO_CONTEM = [c.strip().upper() for c in _os.getenv("PROJETO_CENTRO_CUSTO_CONTEM", "PRJ").split(",")
                               if c.strip()]
# Centro de custo x centro (pedido do João, 28/10/2026, após reunião com o Contábil): no SAP o centro de custo só
# se liga à empresa, não ao centro. A regra vem da base de centros de custo (Base_Centro_de_Custos_v3 - Carga SAP,
# aba Planilha1): os 2 primeiros dígitos do centro de custo são o código da empresa/filial (CodEmpresa).
# Ex.: 01PRJ040 e 01C1010101 = tipo A Matriz (1001); 12PRJ044 = tipo A Filial PR (1003). A tabela fica no config.py;
# centro fora dela não é conferido. Desligar com BLOQUEIO_CENTRO_CUSTO=0.
BLOQUEIO_CENTRO_CUSTO = _os.getenv("BLOQUEIO_CENTRO_CUSTO", "1").strip() == "1"
from config import CC_PREFIXO_POR_CENTRO   # tabela centro -> prefixo do centro de custo (config.py)


def _erro_centro_custo(item: dict) -> str:
    """Mensagem de bloqueio se algum centro de custo do item não é do centro (filial) do item; senão ""."""
    centro = str(item.get("plant") or "").strip()
    prefixo = CC_PREFIXO_POR_CENTRO.get(centro)
    if not prefixo:
        return ""
    ccs = [c for c in (item.get("centros_custo") or [item.get("centro_custo")]) if str(c or "").strip()]
    errados = [c for c in ccs if not str(c).strip().upper().startswith(prefixo)]
    if not errados:
        return ""
    return (f"Bloqueio de Centro de Custo -- Inconsistência no item {item.get('po_item')} do Pedido de Compras "
            f"{item.get('po_number')}: centro de custo {', '.join(errados)} não pertence ao centro {centro} (pela base de "
            f"centros de custo, os do centro {centro} começam com {prefixo}). Corrija o centro de custo no Pedido de Compras "
            "no SAP; a automação não lançou a nota.")


NOMES_UTILIZACAO = {"0": "Revenda", "1": "Industrialização", "2": "Consumo", "3": "Imobilizado", "4": "Consumo ativ. principal"}


def _ler_utilizacao_por_centro(texto: str) -> dict:
    mapa = {}
    for par in (texto or "").split(","):
        if ":" not in par:
            continue
        centro, usos = par.split(":", 1)
        usos = {u.strip() for u in usos.split("|") if u.strip()}
        if centro.strip() and usos:
            mapa[centro.strip()] = usos
    return mapa


PROJETO_UTILIZACAO_POR_CENTRO = _ler_utilizacao_por_centro(
    _os.getenv("PROJETO_UTILIZACAO_POR_CENTRO", "1001:2,1002:3,1003:3"))


def _erro_utilizacao_projeto(item: dict) -> str:
    """Mensagem de bloqueio se o item é de projeto e a utilização não é a do centro; senão ""."""
    classif = (item.get("account_category") or "").upper()
    cc = (item.get("centro_custo") or "").upper()
    cc_projeto = bool(cc) and any(t in cc for t in PROJETO_CENTRO_CUSTO_CONTEM)
    if not (classif in PROJETO_CLASSIFICACOES and cc_projeto):
        return ""
    centro = (item.get("plant") or "").strip()
    esperadas = PROJETO_UTILIZACAO_POR_CENTRO.get(centro)
    if not esperadas:
        return ""
    uso = (item.get("material_usage") or "").strip()
    if uso in esperadas:
        return ""
    lista = " ou ".join(f"{u} ({NOMES_UTILIZACAO.get(u, u)})" for u in sorted(esperadas))
    origem = f"classificação contábil {classif}, centro de custo {cc}, centro {centro}"
    return (f"Bloqueio de Utilização (Projeto) -- Inconsistência no item {item.get('po_item')} do Pedido de Compras "
            f"{item.get('po_number')}: o item é de projeto ({origem}), mas está com Utilização "
            f"'{uso or 'não preenchida'}'. Pedido de projeto do centro {centro} deve ter Utilização {lista}. "
            "Corrija a Utilização no Pedido de Compras no SAP; a automação não lançou a nota.")


# Utilização obrigatória em todo item do pedido que entra na nota (pedido do João, 19/10/2026: a NF 13176936 foi
# lançada com o item 10 sem Utilização e só a Auditoria POP avisou depois). Vale no lançamento e na validação da
# Logística. Item de projeto continua com a regra própria acima. Desligar com BLOQUEIO_UTILIZACAO_VAZIA=0.
BLOQUEIO_UTILIZACAO_VAZIA = _os.getenv("BLOQUEIO_UTILIZACAO_VAZIA", "1").strip() == "1"


def _erro_utilizacao_vazia(payload: dict, itens_po: list) -> str:
    """Mensagem de bloqueio se algum item do pedido usado na fatura está sem Utilização; senão ""."""
    usados = {(str(l.get("PurchaseOrder", "")).lstrip("0"), str(l.get("PurchaseOrderItem", "")).lstrip("0"))
              for l in (payload or {}).get("to_SuplrInvcItemPurOrdRef", [])}
    sem, vistos = [], set()
    for it in itens_po or []:
        chave = (str(it.get("po_number", "")).lstrip("0"), str(it.get("po_item", "")).lstrip("0"))
        if chave in usados and chave not in vistos and not str(it.get("material_usage") or "").strip():
            vistos.add(chave)
            sem.append(f"item {it.get('po_item')} do Pedido {it.get('po_number')}")
    if not sem:
        return ""
    return ("Bloqueio de Utilização -- Inconsistência no item do Pedido de Compras: Utilização do material não "
            f"preenchida ({', '.join(sem)}). Preencha a Utilização no Pedido de Compras no SAP conforme o POP de "
            "classificação fiscal (0 Revenda, 1 Industrialização, 2 Consumo, 3 Imobilizado, 4 Consumo ativ. principal); "
            "a automação não lançou a nota.")


def _retencoes_do_cadastro_nfse(card_data: dict, company_code: str) -> list:
    """NFS-e: linhas de to_SupplierInvoiceWhldgTax a partir do cadastro do fornecedor.
    A API NÃO copia as retenções do cadastro (teste no QAS em 21/10: fatura 5141451831 criada sem nenhuma, com
    CA/C1, PA/P1, RF/1R e SA/S1 sujeitos no cadastro); o app copia. Regra do app medida em produção
    (retencoes_fatura_x_cadastro.py, faturas com pedido e sem documento fiscal desde 01/09): só os tipos marcados
    como SUJEITOS entram na fatura, com o código do cadastro, sem base nem valor (o SAP calcula) -- 252 de 256.
    Tipo sem código no cadastro fica de fora."""
    pedidos = re.findall(r"\d+", str(card_data.get("numero_pedido_sap", "")))
    if not pedidos:
        return []
    headers, kwargs = {**_auth_headers(), "Accept": "application/json"}, _auth_kwargs()
    base_po = SAP_BASE_URL.rstrip("/") + "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV"
    base_bp = SAP_BASE_URL.rstrip("/") + "/sap/opu/odata/sap/API_BUSINESS_PARTNER"
    try:
        r = requests.get(f"{base_po}/A_PurchaseOrder('{pedidos[0].zfill(10)}')",
                         params={"$format": "json", "$select": "Supplier"}, headers=headers, **kwargs, timeout=30)
        r.raise_for_status()
        fornecedor = r.json().get("d", {}).get("Supplier", "")
        r = requests.get(f"{base_bp}/A_SupplierWithHoldingTax",
                         params={"$format": "json", "$filter": f"Supplier eq '{fornecedor}' and CompanyCode eq '{company_code}'"},
                         headers=headers, **kwargs, timeout=30)
        r.raise_for_status()
        cadastro = r.json().get("d", {}).get("results", [])
    except Exception as e:
        raise ValueError(f"Nota de serviço: não foi possível ler as retenções do cadastro do fornecedor no SAP ({e}). "
                         "A automação não lançou a nota para não lançar sem retenção.")
    linhas = [{"WithholdingTaxType": w["WithholdingTaxType"], "DocumentCurrency": "BRL",
               "WithholdingTaxCode": w["WithholdingTaxCode"]}
              for w in cadastro if w.get("IsWithholdingTaxSubject") and w.get("WithholdingTaxCode")]
    log.info("NFS-e: retenções do cadastro do fornecedor %s (%s): %s", fornecedor, company_code,
             " + ".join(f"{l['WithholdingTaxType']}/{l['WithholdingTaxCode']}" for l in linhas) or "nenhuma sujeita")
    return linhas


def _casar_itens(dados_nf: dict, card_data: dict, company_config: dict):
    """Casamento NF x pedido do card, como sempre: 1:1 -> 1:N -> rateio por valor.
    Devolve (payload, usou_1_to_n, rateio_por_valor, banco_de_migos)."""
    usou_1_to_n = False
    rateio_por_valor = False
    # Criação do banco de MIGOs secundário para o self-healing
    banco_de_migos = {}
    for item in card_data.get("itens_po", []):
        if item.get("lista_migos_disponiveis"):
            chave = f"{item['po_number']}-{item['po_item']}"
            banco_de_migos[chave] = list(item["lista_migos_disponiveis"])

    try:
        payload = _montar_payload_1_to_1(dados_nf, card_data, company_config, banco_de_migos)
    except ValueError as ve_1to1:
        if "MATCH_FAIL:" in str(ve_1to1):
            log.info("Match 1:1 falhou por quebra de saldo/item. Acionando Fallback para Rateio 1:N...")
            try:
                banco_de_migos = {}
                for item in card_data.get("itens_po", []):
                    if item.get("lista_migos_disponiveis"):
                        chave = f"{item['po_number']}-{item['po_item']}"
                        banco_de_migos[chave] = list(item["lista_migos_disponiveis"])

                payload = _montar_payload_1_to_n(dados_nf, card_data, company_config, banco_de_migos)
                usou_1_to_n = True
            except ValueError as ve_1toN:
                # Último recurso: rateio por VALOR (NF em pacote x Pedido em unidade). Só roda aqui,
                # quando 1:1 e 1:N já falharam -- não muda nada nos casos que já lançam.
                if "Não há itens compatíveis" not in str(ve_1toN):
                    raise ValueError(str(ve_1toN).replace("MATCH_FAIL: ", ""))
                try:
                    banco_de_migos = {}
                    for item in card_data.get("itens_po", []):
                        if item.get("lista_migos_disponiveis"):
                            chave = f"{item['po_number']}-{item['po_item']}"
                            banco_de_migos[chave] = list(item["lista_migos_disponiveis"])
                    payload = _montar_payload_1_to_n(dados_nf, card_data, company_config, banco_de_migos,
                                                     rateio_por_valor=True)
                    usou_1_to_n = True
                    rateio_por_valor = True
                    log.info("Rateio 1:N falhou por preço unitário; lançando por rateio de VALOR (unidade da NF diferente do Pedido).")
                except ValueError as ve_valor:
                    if "Não há itens compatíveis" in str(ve_valor):
                        raise ValueError(str(ve_1toN).replace("MATCH_FAIL: ", ""))
                    raise ValueError(str(ve_valor).replace("MATCH_FAIL: ", ""))
        else:
            raise
    return payload, usou_1_to_n, rateio_por_valor, banco_de_migos


def _aplicar_fase1(session, dados_nf: dict, card_data: dict, condicoes_encontradas: list):
    """Fase 1 (condição de pagamento dos pedidos x vencimentos do XML). Grava a condição escolhida em
    card_data (payment_terms / cond_pagamento) ou levanta ValueError. Mesmo código que rodava inline."""
    duplicatas_xml = dados_nf.get("duplicatas", [])
    
    lista_bloqueio = ["compra_site", "adiantamento", "agenciamento", "cartoes", "cte"]
    tipo_op = card_data.get("tipo_operacao", "normal")
    
    if not (_operacoes_do_card(card_data) & set(lista_bloqueio)) and condicoes_encontradas:
        info_fase1 = {}
        args_fase1 = dict(
            session=session, 
            condicoes_pedidos=condicoes_encontradas, 
            duplicatas_xml=duplicatas_xml, 
            data_emissao_raw=dados_nf.get("data_emissao_raw"),
            valor_total_xml=float(dados_nf.get("valor_total", 0)),
            valor_ipi_xml=float(dados_nf.get("valor_ipi_total", 0) or 0),
            valor_frete_xml=float(dados_nf.get("valor_frete_total", 0) or 0),
            info=info_fase1,
        )
        try:
            cond_correta = _validar_fase1_compras(**args_fase1)
        except ValueError:
            # Retenções na NF (retTrib: PIS/COFINS/CSLL/IR/INSS retidos) ou desconto na fatura: as
            # duplicatas somam o valor LÍQUIDO cobrado (cobr/fat/vLiq), não o vNF. Só nesse caso
            # tenta de novo com esse valor -- o que já passava com o vNF continua igual.
            try:
                v_liq = float(dados_nf.get("valor_liquido_fatura") or 0)
            except ValueError:
                v_liq = 0.0
            if v_liq <= 0 or abs(v_liq - args_fase1["valor_total_xml"]) < 0.01:
                raise
            cond_correta = _validar_fase1_compras(**{**args_fase1, "valor_total_xml": v_liq})
            log.info(f"Fase 1: condição {cond_correta} aceita pelo valor líquido da fatura (vLiq R$ {v_liq:.2f}; "
                     f"vNF R$ {args_fase1['valor_total_xml']:.2f} -- diferença de retenções/desconto).")
        if info_fase1.get("parcela_antecipada"):
            card_data["fase1_parcela_antecipada"] = info_fase1["parcela_antecipada"]
        card_data["payment_terms"] = cond_correta
        card_data["cond_pagamento"] = cond_correta
    elif condicoes_encontradas:
        card_data["payment_terms"] = condicoes_encontradas[0]
        card_data["cond_pagamento"] = condicoes_encontradas[0]


def _checar_bloqueios_item(item: dict, bp_xml: str, eh_frete: bool):
    """Bloqueios por item do pedido (projeto/utilização, filial, ativo, centro de custo). Levanta ValueError."""
    if BLOQUEIO_CENTRO_CUSTO:
        erro_cc = _erro_centro_custo(item)
        if erro_cc:
            raise ValueError(erro_cc)
    if BLOQUEIO_PROJETO_UTILIZACAO and not eh_frete:
        erro_projeto = _erro_utilizacao_projeto(item)
        if erro_projeto:
            raise ValueError(erro_projeto)
    plant_po = item.get("plant")
    if plant_po and plant_po != bp_xml:
        raise ValueError(
            f"Bloqueio de Filial: A nota fiscal foi emitida para a filial {bp_xml} (CNPJ do XML), "
            f"mas o item {item['po_item']} do Pedido de Compras {item['po_number']} pertence ao Centro Logístico {plant_po}. "
            "Para evitar erros contábeis e fiscais, a automação não prosseguirá. Corrija o Pedido de Compras no SAP ou solicite o cancelamento do documento fiscal."
        )
        
    asset_id = item.get("master_fixed_asset")
    asset_plant = item.get("asset_plant")
    if asset_id and asset_plant and asset_plant != bp_xml:
        raise ValueError(
            f"Bloqueio de Filial (Ativo Imobilizado): A nota fiscal foi emitida para a filial {bp_xml}, "
            f"mas o Ativo {asset_id} apontado no item {item['po_item']} do Pedido de Compras "
            f"pertence ao Centro de Custo/Logístico {asset_plant}. "
            "Corrija a classificação contábil no Pedido de Compras antes de prosseguir com o faturamento."
        )


def lancar_fatura_completo(dados_nf: dict, card_data: dict, company_config: dict, somente_validar: bool = False) -> dict:
    """Lança a fatura da NF no SAP (Fase 1 -> casamento dos itens -> criação -> registro -> auditorias).
    somente_validar=True (validação na Logística, validacao_logistica.py): roda as MESMAS checagens do
    lançamento (filial, projeto, condição de pagamento, CNPJ, NCM/preço/saldo do pedido) e para antes de
    enviar ao SAP. O recebimento (MIGO) ainda não existe nessa fase, então não é exigido
    (card_data["validacao_sem_recebimento"]). Devolve {"validado": True, ...} ou levanta ValueError."""
    usou_1_to_n = False
    rateio_por_valor = False
    aviso_pedido_extra = ""
    session = requests.Session()
    
    bp_xml = company_config.get("business_place")
    # Frete (CT-e / etiqueta FRETE) não entra na regra de utilização de projeto, só nota de material
    # (analista fiscal, Fiscal, 26/10/2026, card 1424415133, NF 3981492, pedido 4546761242, centro 1003).
    eh_frete = dados_nf.get("tipo") == "CTE" or "cte" in _operacoes_do_card(card_data)
    card_data.pop("itens_po_extra", None)

    # --- FASE 1: BARREIRA COMERCIAL (Notificação Compras) ---
    _aplicar_fase1(session, dados_nf, card_data, card_data.get("condicoes_encontradas", []))

    try:
        payload, usou_1_to_n, rateio_por_valor, banco_de_migos = _casar_itens(dados_nf, card_data, company_config)
    except ValueError as erro_match:
        # Pedido indicado no XML e fora do card: última tentativa, só quando faltou item compatível.
        if not (PEDIDO_EXTRA_XML and "Não há itens compatíveis" in str(erro_match)):
            raise
        extras = _carregar_pedidos_extra_xml(dados_nf, card_data, company_config) if _pedidos_xml_fora_do_card(dados_nf, card_data) else []
        if not extras:
            raise
        card_data["itens_po_extra"] = extras
        try:
            banco_de_migos = _banco_de_migos(card_data)
            payload = _montar_payload_1_to_n(dados_nf, card_data, company_config, banco_de_migos, rateio_por_valor=True)
            usou_1_to_n, rateio_por_valor = True, True
        except ValueError as erro_extra:
            card_data.pop("itens_po_extra", None)
            log.info("Pedido do XML fora do card também não resolveu (%s); mantenho o erro original.", erro_extra)
            raise erro_match
        usados = {(str(l.get("PurchaseOrder", "")).lstrip("0"), str(l.get("PurchaseOrderItem", "")).lstrip("0"))
                  for l in payload.get("to_SuplrInvcItemPurOrdRef", [])}
        extras_usados = [x for x in extras
                         if (str(x["po_number"]).lstrip("0"), str(x["po_item"]).lstrip("0")) in usados]
        if not extras_usados:
            card_data.pop("itens_po_extra", None)
            raise erro_match
        try:
            for item in extras_usados:
                _checar_bloqueios_item(item, bp_xml, eh_frete)
            # Condição de pagamento: o pedido extra passa pela mesma Fase 1 que passaria se estivesse no card.
            cond_card = list(card_data.get("condicoes_encontradas", []) or [])
            cond_extra = [c for x in extras_usados for c in (x.get("_condicoes_pedido_extra") or [])]
            if any(c not in cond_card for c in cond_extra):
                _aplicar_fase1(session, dados_nf, card_data, list(dict.fromkeys(cond_card + cond_extra)))
                cond_final = str(card_data.get("cond_pagamento") or card_data.get("payment_terms") or "").upper().strip()
                if cond_final and payload.get("PaymentTerms") != cond_final:
                    log.info("Fase 1 refeita com o pedido do XML: condição %s -> %s.", payload.get("PaymentTerms"), cond_final)
                    payload["PaymentTerms"] = cond_final
        except ValueError:
            card_data.pop("itens_po_extra", None)
            raise
        aviso_pedido_extra = ("Pedido indicado no XML e fora do card: "
                              + ", ".join(f"item {x['po_item']} do pedido {x['po_number']}" for x in extras_usados)
                              + ". Conferir se o pedido deveria estar no card.")
        log.info(aviso_pedido_extra)

    # Bloqueios por item (projeto/utilização, filial, ativo) só nos itens do pedido do card que entram na
    # fatura (pedido do João, 27/10/2026). Antes rodavam em todos os itens em aberto do pedido, antes do
    # casamento: uma NF que usava só o item 20 era barrada por um problema no item 40. Os itens do pedido
    # indicado no XML já foram conferidos acima.
    _itens_na_fatura = {(str(l.get("PurchaseOrder", "")).lstrip("0"), str(l.get("PurchaseOrderItem", "")).lstrip("0"))
                        for l in payload.get("to_SuplrInvcItemPurOrdRef", [])}
    for item in card_data.get("itens_po", []):
        # Sem linhas de pedido no payload (não deveria acontecer), confere todos, como antes.
        if not _itens_na_fatura or (str(item.get("po_number", "")).lstrip("0"), str(item.get("po_item", "")).lstrip("0")) in _itens_na_fatura:
            _checar_bloqueios_item(item, bp_xml, eh_frete)

    # Nota de serviço fica fora: em produção 72% dos itens de pedido de serviço com IVA 10 não têm utilização
    # (utilizacao_pedidos_servico.py, 21/10) e a NFS-e é lançada sem documento fiscal. Decisão do João em 21/10.
    if BLOQUEIO_UTILIZACAO_VAZIA and dados_nf.get("tipo") != "NFSE":
        erro_utilizacao = _erro_utilizacao_vazia(payload, list(card_data.get("itens_po", [])) + list(card_data.get("itens_po_extra", [])))
        if erro_utilizacao:
            raise ValueError(erro_utilizacao)

    retencoes_nfse = []
    if dados_nf.get("tipo") == "NFSE":
        retencoes_nfse = _retencoes_do_cadastro_nfse(card_data, company_config.get("company_code", ""))
        if retencoes_nfse:
            payload["to_SupplierInvoiceWhldgTax"] = retencoes_nfse

    if somente_validar:
        if card_data.get("itens_po_extra"):
            card_data["itens_po"] = list(card_data.get("itens_po", [])) + [
                x for x in card_data.pop("itens_po_extra") if x.get("xml_item_nf")]
        return {"validado": True, "usou_1_to_n": usou_1_to_n, "rateio_por_valor": rateio_por_valor,
                "aviso_pedido_extra": aviso_pedido_extra,
                "payload": payload, "payment_terms": card_data.get("payment_terms", "")}

    # Self-Healing para msg SAP 8B/246 ("Nota fiscal já criada para entrada de mercadorias"):
    # o recebimento referenciado já tem NF vinculada no SAP. Só resolve trocando para OUTRO
    # recebimento do mesmo item; se não houver, sobe o erro com orientação (_ORIENTACAO_8B246).
    doc_data = None
    ultimo_erro_sap = None
    max_tentativas_migo = 100
    
    for rodada_migo in range(1, max_tentativas_migo + 1):
        try:
            if retencoes_nfse:
                payload["to_SupplierInvoiceWhldgTax"] = retencoes_nfse  # o payload pode ter sido remontado (troca de MIGO)
            doc_data = criar_fatura(payload)
            break 
        except ValueError as erro_sap:
            ultimo_erro_sap = str(erro_sap)
            if "criada para entrada de mercadorias" in ultimo_erro_sap.lower():
                log.warning(f"SAP recusou: o recebimento (MIGO) referenciado já possui Nota Fiscal vinculada (msg 8B/246) (Rodada {rodada_migo}). Tentando outra referência de MIGO, se houver...")
                
                houve_troca = False
                if usou_1_to_n:
                    try:
                        payload = _montar_payload_1_to_n(dados_nf, card_data, company_config, banco_de_migos,
                                                         rateio_por_valor=rateio_por_valor)
                        houve_troca = True
                    except Exception:
                        pass
                else:
                    try:
                        payload = _montar_payload_1_to_1(dados_nf, card_data, company_config, banco_de_migos)
                        houve_troca = True
                    except Exception:
                        pass
                        
                if houve_troca:
                    log.info("Nova MIGO injetada com sucesso no Payload. Retentando disparo...")
                    continue
                else:
                    raise ValueError(ultimo_erro_sap + _ORIENTACAO_8B246)
            else:
                raise erro_sap

    if not doc_data:
        if ultimo_erro_sap:
            if "criada para entrada de mercadorias" in ultimo_erro_sap.lower():
                raise ValueError(ultimo_erro_sap + _ORIENTACAO_8B246)
            raise ValueError(ultimo_erro_sap)
        raise ValueError("Falha na comunicação com o SAP durante o envio do Payload.")

    doc_num, doc_year = doc_data.get("SupplierInvoice", ""), doc_data.get("FiscalYear", "")
    # Itens do pedido do XML que entraram na fatura passam a fazer parte da lista do card
    # (Auditoria POP, natureza do frete etc. olham card_data["itens_po"]).
    if card_data.get("itens_po_extra"):
        card_data["itens_po"] = list(card_data.get("itens_po", [])) + [
            x for x in card_data.pop("itens_po_extra") if x.get("xml_item_nf")]
    attempts_used = doc_data.get("attempts_used", 1)
    
    score_risco = _calcular_risk_score(card_data, dados_nf, attempts_used, usou_1_to_n)
    
    posted = False
    dados_fiscais_cds = []
    alerta_financeiro = ""
    
    if doc_num:
        try:
            posted = registrar_fatura(doc_num, doc_year)
            
            # NFS-e é lançada sem documento fiscal (padrão do lançamento manual): não há NF a buscar.
            nf_interna = "" if dados_nf.get("tipo") == "NFSE" else _buscar_nf_do_documento(session, doc_num, doc_year)
            if nf_interna:
                dados_fiscais_cds = _buscar_dados_fiscais_cds(session, nf_interna)
                
                if dados_nf.get("tipo") == "CTE":
                    jur_ini = dados_nf.get("jur_ini", "")
                    jur_fim = dados_nf.get("jur_fim", "")
                    freight_nature = ""
                    
                    if company_config.get("pop_type") == "tipo_a" and card_data.get("itens_po"):
                        uso = str(card_data["itens_po"][0].get("material_usage", "")).strip()
                        if uso == "1": freight_nature = "2"
                        elif uso == "0": freight_nature = "0"
                        elif uso == "3": freight_nature = "9"
                        
                    # PATCH em A_BR_NFDocument/A_BR_NFItem só funciona em NF criada pela API de Nota Fiscal
                    # (J_1BNFDOC-API = 'X'); as nossas vêm da API de fatura -> sempre HTTP 400 (suporte SAP,
                    # 14/10/2026). Desligado: o CT-e continua parado para o ajuste manual. CTE_ATUALIZAR_CAMPOS=1 religa.
                    if CTE_ATUALIZAR_CAMPOS:
                        _atualizar_cte_sap(session, nf_interna, jur_ini, jur_fim, freight_nature)
            
            # --- FASE 2: AUDITORIA FINANCEIRA DO PASSIVO REAL (Notificação Contas a Pagar) ---
            try:
                alerta_financeiro = _auditar_fase2_financeiro(session, doc_num, doc_year, company_config["company_code"], dados_nf)
            except Exception as e:
                log.warning("Erro inesperado na Fase 2 de Auditoria Financeira: %s", e)

            # --- Ajuste das parcelas adiado para a fase Pagamento: aqui o alerta fica só informativo ---
            if FASE3_NO_PAGAMENTO:
                if alerta_financeiro:
                    alerta_financeiro = (alerta_financeiro.replace("\n_Ação do Financeiro estritamente necessária no SAP._", "")
                                         + "\n" + AVISO_AJUSTE_NO_PAGAMENTO)

            # --- FASE 3: vencimento ou valor divergente -> corrige as parcelas e reaudita ---
            alerta_original, res_f3, restante = alerta_financeiro, {}, None
            if not FASE3_NO_PAGAMENTO and alerta_financeiro and (FASE3_AJUSTAR_DATAS or FASE3_REPARCELAR):
                try:
                    res_f3 = _fase3_ajustar_parcelas(doc_num, doc_year, company_config["company_code"], dados_nf, card_data)
                    if res_f3:
                        restante = _auditar_fase2_financeiro(session, doc_num, doc_year, company_config["company_code"], dados_nf)
                        alerta_financeiro = "\n\n".join(t for t in (_texto_fase3(res_f3, doc_year), restante) if t)
                except Exception as e:
                    log.warning("Erro inesperado na Fase 3 (ajuste de parcelas): %s", e)

            # --- Adiantamentos do card pagos antes do lançamento (qualquer parcela) ---
            # Só com as parcelas iguais à NF: sem alerta na Fase 2, ou a Fase 3 ajustou e não sobrou divergência.
            parcelas_iguais_nf = not alerta_original or (bool(res_f3) and not res_f3.get("falha") and not restante)
            if not FASE3_NO_PAGAMENTO and FASE3_ADIANTAMENTO_CARD and parcelas_iguais_nf:
                try:
                    res_adto = _compensar_adiantamentos_card(doc_num, doc_year, company_config["company_code"],
                                                             dados_nf, card_data, res_f3)
                    alerta_financeiro = "\n\n".join(t for t in (alerta_financeiro, _texto_adiantamentos_card(res_adto)) if t)
                except Exception as e:
                    log.warning("Erro inesperado na compensação de adiantamentos do card: %s", e)

        except Exception as e:
            log.warning("Erro durante PostInvoice, Auditoria POP ou Auditoria Financeira: %s", e)
            
    return {
        "supplier_invoice": doc_num, 
        "fiscal_year": doc_year, 
        "posted": posted, 
        "risk_score": score_risco, 
        "dados_fiscais_cds": dados_fiscais_cds,
        "alerta_financeiro": alerta_financeiro,
        "aviso_pedido_extra": aviso_pedido_extra,
        "raw": doc_data
    }