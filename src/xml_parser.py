"""
xml_parser.py — Parseia NF-e (mod=55/65), CT-e e NFS-e
Retorna sempre um dicionário normalizado com os campos necessários para o SAP.
Sem chutes: Dados ausentes ou inválidos resultam em erro claro e bloqueio.
"""

from lxml import etree
from datetime import datetime, timezone
import re

NS_NFE = "http://www.portalfiscal.inf.br/nfe"
NS_CTE = "http://www.portalfiscal.inf.br/cte"
NS_NFSE_NAC = "http://www.sped.fazenda.gov.br/nfse"   # NFS-e padrão nacional (Sefin Nacional)


def _limpar_xml_sujo(xml_bytes: bytes) -> bytes:
    """
    Remove tags malformadas injetadas por ERPs ruins (ex: <xmlns:ns0>...</xmlns:ns0>)
    que quebram o parser do lxml e até dos navegadores web.
    """
    try:
        # Converte para string para usar regex (ignorando erros de encoding estranhos)
        xml_str = xml_bytes.decode('utf-8', errors='ignore')
        
        # Remove a tag bizarra <xmlns:ns0> e seu conteúdo
        xml_str_limpo = re.sub(r'<xmlns:ns0>.*?</xmlns:ns0>', '', xml_str)
        
        # Volta para bytes
        return xml_str_limpo.encode('utf-8')
    except Exception:
        # Se falhar a limpeza, retorna o original e deixa o erro estourar naturalmente
        return xml_bytes

def detectar_tipo(xml_bytes: bytes) -> str:
    raiz = etree.fromstring(xml_bytes)
    ns   = raiz.nsmap.get(None, "")
    if "portalfiscal.inf.br/cte" in ns:
        return "CTE"
    if "portalfiscal.inf.br/nfe" in ns:
        return "NFE"
    if raiz.find(f".//{{{NS_CTE}}}CTe") is not None:
        return "CTE"
    if raiz.find(f".//{{{NS_NFE}}}NFe") is not None:
        return "NFE"
    # NFS-e padrão nacional (namespace da Sefin Nacional)
    if raiz.tag == f"{{{NS_NFSE_NAC}}}evento":
        # Evento da NFS-e (no zip de 1055 XMLs de jul/26 eram todos cancelamentos). Não é a nota.
        desc = raiz.find(f".//{{{NS_NFSE_NAC}}}xDesc")
        chave = raiz.find(f".//{{{NS_NFSE_NAC}}}chNFSe")
        desc = desc.text.strip() if desc is not None and desc.text else "evento"
        chave = chave.text.strip() if chave is not None and chave.text else "?"
        raise ValueError(f"XML inválido: o arquivo anexado é um evento da NFS-e ({desc}, chave {chave}), não a nota. "
                         "Se a nota foi cancelada, não lançar; se não, anexar o XML da própria NFS-e.")
    if NS_NFSE_NAC in ns or raiz.find(f".//{{{NS_NFSE_NAC}}}infNFSe") is not None:
        return "NFSE"
    # NFS-e municipal (ABRASF) — sem namespace padrão
    if raiz.find(".//InfNfse") is not None or raiz.find(".//Numero") is not None:
        return "NFSE"
    raise ValueError("Tipo de documento fiscal não reconhecido no XML. Certifique-se de ser um XML válido de NF-e, CT-e ou NFS-e.")


def _ts_para_date_sap(valor: str) -> str | None:
    """
    Converte datas do XML para o formato /Date(ms)/ do OData SAP.
    Ignora horas e fusos para evitar que notas da noite pulem para o dia seguinte no UTC.
    """
    if not valor:
        return None
    try:
        # Pega apenas os 10 primeiros caracteres: YYYY-MM-DD
        data_str = valor[:10]
        # Força a data a ser interpretada como 00:00:00 cravado em UTC
        dt = datetime.strptime(data_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return f"/Date({int(dt.timestamp()) * 1000})/"
    except Exception:
        return None


def _extrair_hora_sap(valor: str) -> str:
    if not valor or "T" not in valor:
        return "PT00H00M00S"
    try:
        h, m, s = valor.split("T")[1][:8].split(":")
        return f"PT{int(h):02d}H{int(m):02d}M{int(s):02d}S"
    except Exception:
        return "PT00H00M00S"


def _numero_aleatorio_nfe(chave: str) -> str:
    """Posições 35-42 da chave de acesso (cNF, 8 dígitos, sem o dígito verificador)."""
    chave = re.sub(r"\D", "", chave)
    if len(chave) == 44:
        return chave[35:43]
    return chave[-9:-1] if len(chave) >= 9 else chave


def _uf_por_codigo(codigo: str) -> str:
    mapa = {
        "11":"RO","12":"AC","13":"AM","14":"RR","15":"PA","16":"AP","17":"TO",
        "21":"MA","22":"PI","23":"CE","24":"RN","25":"PB","26":"PE","27":"AL",
        "28":"SE","29":"BA","31":"MG","32":"ES","33":"RJ","35":"SP","41":"PR",
        "42":"SC","43":"RS","50":"MS","51":"MT","52":"GO","53":"DF",
    }
    return mapa.get(str(codigo)[:2], "")


# ─── helpers de xpath com namespace ──────────────────────────────────────────

def _g(raiz_el, xpath: str, ns: str) -> str:
    """
    Percorre um xpath do tipo 'pai/filho/neto' adicionando o namespace
    em cada segmento. Funciona independentemente da profundidade.
    """
    el = raiz_el
    for parte in xpath.split("/"):
        if el is None:
            return ""
        el = el.find(f"{{{ns}}}{parte}")
    return el.text.strip() if el is not None and el.text else ""


# ─── extração de itens (multi-item) ─────────────────────────────────────────────

def _extrair_itens(inf_el, ns: str) -> list:
    """Extrai todos os <det> e retorna lista de dicts com dados de cada item."""
    itens = []
    for det in inf_el.findall(f"{{{ns}}}det"):
        prod = det.find(f"{{{ns}}}prod")
        if prod is None:
            continue
        def gp(tag):
            el = prod.find(f"{{{ns}}}{tag}")
            return el.text.strip() if el is not None and el.text else ""
            
        def gi(tag):
            # Procura em todo o bloco <imposto> do item
            imposto = det.find(f"{{{ns}}}imposto")
            if imposto is not None:
                 el = imposto.find(f".//{{{ns}}}{tag}")
                 return el.text.strip() if el is not None and el.text else "0.00"
            return "0.00"
        
        seq = det.get("nItem", str(len(itens) + 1))
        qtd = gp("qCom")
        unidade = gp("uCom")
        preco_unit = gp("vUnCom")
        
        # --- CÁLCULO BLINDADO DO VALOR LÍQUIDO DO ITEM ---
        v_prod_bruto = gp("vProd") or "0.00"
        v_desc       = gp("vDesc") or "0.00"
        v_frete      = gp("vFrete") or "0.00"
        v_seg        = gp("vSeg") or "0.00"
        v_outro      = gp("vOutro") or "0.00"
        
        try:
            valor_liquido = float(v_prod_bruto) - float(v_desc) + float(v_seg) + float(v_outro)
            valor = f"{valor_liquido:.2f}"
        except Exception:
            valor = v_prod_bruto
        # -------------------------------------------------
        
        if not qtd or not unidade or not valor or not preco_unit:
            raise ValueError(f"XML inválido: O item {seq} da nota não possui quantidade, unidade de medida, valor ou preço unitário declarados.")

        # --- EXTRAÇÃO DE IMPOSTOS ---
        v_icms = float(gi("vICMS") or 0.0)
        v_icmsst = float(gi("vICMSST") or 0.0)

        itens.append({
            "seq":            seq,
            "cfop":           gp("CFOP"),
            "codigo":         gp("cProd"),   
            "descricao":      gp("xProd"),
            "ncm":            gp("NCM"),     
            "quantidade":     qtd,
            "unidade":        unidade,
            "valor":          valor,
            "preco_unitario": preco_unit, 
            "vIPI":           gi("vIPI"),
            "vICMS":          str(v_icms + v_icmsst),
            "vPIS":           gi("vPIS"),
            "vCOFINS":        gi("vCOFINS"),
            # Pedido/item do pedido informados pelo fornecedor (opcionais no layout NF-e).
            # Quando presentes, o motor de correspondência dá prioridade a esse item do Pedido.
            "pedido_xml":      gp("xPed"),
            "item_pedido_xml": gp("nItemPed"),
        })
    
    if not itens:
        raise ValueError("XML inválido: Nenhum produto/item foi encontrado dentro arquivo XML.")
        
    return itens

def _extrair_duplicatas(inf_el, ns: str, valor_total: str = None) -> list:
    """Isola os arrays de faturamento financeiro (<dup>) do XML."""
    duplicatas = []
    cobr = inf_el.find(f".//{{{ns}}}cobr")
    if cobr is not None:
        for dup in cobr.findall(f".//{{{ns}}}dup"):
            nDup = _g(dup, "nDup", ns)
            dVenc = _g(dup, "dVenc", ns)
            vDup = _g(dup, "vDup", ns)
            if dVenc and vDup:
                duplicatas.append({"nDup": nDup, "dVenc": dVenc, "vDup": float(vDup)})

    # Fallback: fornecedores que não preenchem <cobr><dup> (ex.: assinaturas/boletins como a Econet)
    # às vezes só declaram o(s) vencimento(s) real(is) em texto livre no infCpl
    # (ex: "Venc. em 09/09/2026", ou duas ocorrências para duas parcelas). Sem isso, o fluxo
    # assumiria "à vista" e geraria falso positivo de Divergência Comercial. Busca case-insensitive
    # e pega TODAS as ocorrências, na ordem em que aparecem no texto, tratando cada uma como se
    # fosse uma tag <dup> — se houver mais de uma, o valor total é dividido igualmente entre elas
    # (não há como saber o rateio real só pelo texto livre; a última parcela absorve o arredondamento).
    if not duplicatas and valor_total:
        infCpl_el = inf_el.find(f".//{{{ns}}}infCpl")
        texto = infCpl_el.text if infCpl_el is not None and infCpl_el.text else ""
        matches = re.findall(
            r"venc(?:imento)?\.?\s*(?:em)?\s*:?\s*(\d{2})/(\d{2})/(\d{4})",
            texto,
            re.IGNORECASE,
        )
        if matches:
            valor_total_f = float(valor_total)
            n = len(matches)
            valor_parcela = round(valor_total_f / n, 2)
            for idx, (dd, mm, aaaa) in enumerate(matches, start=1):
                if idx == n:
                    # Última parcela absorve a diferença de arredondamento das anteriores.
                    vDup = round(valor_total_f - valor_parcela * (n - 1), 2)
                else:
                    vDup = valor_parcela
                duplicatas.append({"nDup": str(idx), "dVenc": f"{aaaa}-{mm}-{dd}", "vDup": vDup})

    return duplicatas

# ─── NF-e ────────────────────────────────────────────────────────────────────

def parsear_nfe(xml_bytes: bytes) -> dict:
    raiz = etree.fromstring(xml_bytes)
    inf  = raiz.find(f".//{{{NS_NFE}}}infNFe")
    prot = raiz.find(f".//{{{NS_NFE}}}infProt")

    def g(xpath):
        return _g(inf, xpath, NS_NFE)

    chave_acesso = re.sub(r"\D", "", inf.get("Id", "")) if inf is not None else ""
    uf_emitente  = _uf_por_codigo(g("ide/cUF")) or g("emit/enderEmit/UF")

    protocolo = ""
    dh_recbto = ""
    if prot is not None:
        el_prot = prot.find(f"{{{NS_NFE}}}nProt")
        el_dh   = prot.find(f"{{{NS_NFE}}}dhRecbto")
        protocolo = el_prot.text.strip() if el_prot is not None and el_prot.text else ""
        dh_recbto = el_dh.text.strip()   if el_dh   is not None and el_dh.text   else ""

    numero_nf = g("ide/nNF").lstrip("0")
    if not numero_nf:
        raise ValueError("XML inválido: O documento fiscal não possui um número (nNF) preenchido.")
        
    data_emissao_raw = g("ide/dhEmi") or g("ide/dEmi")
    if data_emissao_raw: data_emissao_raw = data_emissao_raw[:10]
    
    data_emissao = _ts_para_date_sap(data_emissao_raw)
    if not data_emissao:
        raise ValueError("XML inválido: A Data de Emissão está ausente ou em formato corrompido.")

    hora_emissao = _extrair_hora_sap(g("ide/dhEmi") or g("ide/dEmi"))
    data_saida = _ts_para_date_sap(g("ide/dhSaiEnt")) 
        
    valor_total = g("total/ICMSTot/vNF")
    if not valor_total:
        raise ValueError("XML inválido: O Valor Total da NF-e (vNF) está ausente.")

    forma_pagamento = g("pag/detPag/tPag") or _g(inf, ".//tPag", NS_NFE)
    
    # EXTRAÇÃO DA DATA DE VENCIMENTO (Pega a primeira duplicata, se houver, mas a array nova 'duplicatas' guiará a tesouraria)
    _el_venc = inf.find(f".//{{{NS_NFE}}}dVenc")
    data_venc = _el_venc.text.strip() if _el_venc is not None and _el_venc.text else ""

    return {
        "tipo":              "NFE",
        "br_nf_type":        "YA",
        "numero_nf":         numero_nf,
        "serie":             g("ide/serie"),
        "data_emissao_raw":  data_emissao_raw,
        "data_emissao":      data_emissao,
        "hora_emissao":      hora_emissao,
        "data_saida_entrada": data_saida,
        "data_vencimento_raw": data_venc,
        "valor_total":       valor_total,
        # Totais usados na Fase 1 para aceitar IPI/frete concentrados na 1ª parcela
        "valor_ipi_total":   g("total/ICMSTot/vIPI") or "0",
        "valor_frete_total": g("total/ICMSTot/vFrete") or "0",
        # Valor líquido cobrado (fatura): pode ser menor que o vNF quando há retenções (retTrib:
        # PIS/COFINS/CSLL/IR/INSS retidos) ou desconto na fatura -- as duplicatas somam este valor.
        "valor_liquido_fatura": g("cobr/fat/vLiq") or "",
        "retencoes": {k: g(f"total/retTrib/{k}") for k in ("vRetPIS", "vRetCOFINS", "vRetCSLL", "vIRRF", "vRetPrev")
                      if (g(f"total/retTrib/{k}") or "0") not in ("", "0", "0.00")},
        "cnpj_emitente":     re.sub(r"\D", "", g("emit/CNPJ")),
        "cnpj_destinatario": re.sub(r"\D", "", g("dest/CNPJ")), 
        "uf_emitente":       uf_emitente,
        "chave_acesso":      chave_acesso,
        "protocolo":         protocolo,
        "data_protocolo":    _ts_para_date_sap(dh_recbto),
        "hora_protocolo":    _extrair_hora_sap(dh_recbto),
        "numero_aleatorio": _numero_aleatorio_nfe(chave_acesso),
        "cfop_xml":         g("det/prod/CFOP"),
        "natureza_operacao": g("ide/natOp"),
        "tp_emis":           g("ide/tpEmis") or "1",
        "quantidade_total":  "", 
        "unidade_medida":    "",
        "forma_pagamento":   forma_pagamento,
        "duplicatas":        _extrair_duplicatas(inf, NS_NFE, valor_total),
        "itens":             _extrair_itens(inf, NS_NFE),
    }


# ─── CT-e ────────────────────────────────────────────────────────────────────

def parsear_cte(xml_bytes: bytes) -> dict:
    raiz = etree.fromstring(xml_bytes)
    inf  = raiz.find(f".//{{{NS_CTE}}}infCte")
    prot = raiz.find(f".//{{{NS_CTE}}}infProt")

    def g(xpath):
        return _g(inf, xpath, NS_CTE)

    chave_acesso = re.sub(r"\D", "", inf.get("Id", "")) if inf is not None else ""
    uf_emitente  = _uf_por_codigo(g("ide/cUF")) or g("emit/enderEmit/UF")

    protocolo = ""
    dh_recbto = ""
    if prot is not None:
        el_prot = prot.find(f"{{{NS_CTE}}}nProt")
        el_dh   = prot.find(f"{{{NS_CTE}}}dhRecbto")
        protocolo = el_prot.text.strip() if el_prot is not None and el_prot.text else ""
        dh_recbto = el_dh.text.strip()   if el_dh   is not None and el_dh.text   else ""

    numero_nf = g("ide/nCT").lstrip("0")
    if not numero_nf:
        raise ValueError("XML inválido: O documento de transporte não possui um número (nCT) preenchido.")
        
    data_emissao_raw = g("ide/dhEmi")
    if data_emissao_raw: data_emissao_raw = data_emissao_raw[:10]
    
    data_emissao = _ts_para_date_sap(data_emissao_raw)
    if not data_emissao:
        raise ValueError("XML inválido: A Data de Emissão do CT-e está ausente ou em formato corrompido.")

    hora_emissao = _extrair_hora_sap(g("ide/dhEmi"))
        
    valor_total = g("vPrest/vTPrest")
    if not valor_total:
        raise ValueError("XML inválido: O Valor Total do serviço (vTPrest) está ausente no CT-e.")

    forma_pagamento = _g(inf, ".//tPag", NS_CTE)
    _el_venc = inf.find(f".//{{{NS_CTE}}}dVenc")
    data_venc = _el_venc.text.strip() if _el_venc is not None and _el_venc.text else ""

    # --- Extração inteligente de impostos do CT-e ---
    imp = inf.find(f".//{{{NS_CTE}}}imp")
    v_icms, v_pis, v_cofins, v_ipi = 0.0, 0.0, 0.0, 0.0
    
    if imp is not None:
        def gi(tag):
            el = imp.find(f".//{{{NS_CTE}}}{tag}")
            return float(el.text.strip()) if el is not None and el.text else 0.0
            
        v_icms = gi("vICMS")
        v_pis = gi("vPIS")
        v_cofins = gi("vCOFINS")
        v_ipi = gi("vIPI")

    # --- REGRA DE OURO: RESOLUÇÃO ESTRITA DO TOMADOR DO FRETE ---
    cnpj_dest  = re.sub(r"\D", "", g("dest/CNPJ"))
    cnpj_rem   = re.sub(r"\D", "", g("rem/CNPJ"))
    cnpj_exped = re.sub(r"\D", "", g("exped/CNPJ"))
    cnpj_receb = re.sub(r"\D", "", g("receb/CNPJ"))
    
    # 1. Lê a tag que diz quem é o legalmente responsável por pagar o frete
    toma_indicator = g("ide/toma3/toma")
    
    if toma_indicator == "0":
        cnpj_tomador = cnpj_rem
    elif toma_indicator == "1":
        cnpj_tomador = cnpj_exped
    elif toma_indicator == "2":
        cnpj_tomador = cnpj_receb
    elif toma_indicator == "3":
        cnpj_tomador = cnpj_dest
    elif toma_indicator == "4":
        cnpj_tomador = re.sub(r"\D", "", g("ide/toma4/CNPJ"))
    else:
        cnpj_tomador = ""

    from config import MAPA_FILIAIS_CNPJ
    
    # 2. Trava Estrita: Se o Tomador não for a nossa empresa, BLOQUEIA!
    if not cnpj_tomador:
        raise ValueError("O XML do CT-e não especifica o Tomador do Serviço (quem paga o frete).")
        
    if cnpj_tomador not in MAPA_FILIAIS_CNPJ:
        raise ValueError(f"CT-e Inválido: A transportadora apontou o CNPJ {cnpj_tomador} como Tomador do Serviço. Como este CNPJ não pertence às nossas filiais, não podemos lançar esta despesa. Solicite a correção à transportadora.")

    cnpj_nosso_grupo = cnpj_tomador

    # EXTRAÇÃO DE ORIGEM E DESTINO DO FRETE (Municípios Formatados)
    c_mun_ini = g("ide/cMunIni")
    c_mun_fim = g("ide/cMunFim")
    jur_ini = f"{_uf_por_codigo(c_mun_ini)} {c_mun_ini}" if c_mun_ini else ""
    jur_fim = f"{_uf_por_codigo(c_mun_fim)} {c_mun_fim}" if c_mun_fim else ""

    return {
        "tipo":              "CTE",
        "br_nf_type":        "C7",
        "numero_nf":         numero_nf,
        "serie":             g("ide/serie"),
        "data_emissao_raw":  data_emissao_raw,
        "data_emissao":      data_emissao,
        "hora_emissao":      hora_emissao,
        "data_vencimento_raw": data_venc,
        "valor_total":       valor_total,
        "cnpj_emitente":     re.sub(r"\D", "", g("emit/CNPJ")),
        "cnpj_destinatario": cnpj_nosso_grupo,
        "uf_emitente":       uf_emitente,
        "chave_acesso":      chave_acesso,
        "protocolo":         protocolo,
        "data_protocolo":    _ts_para_date_sap(dh_recbto),
        "hora_protocolo":    _extrair_hora_sap(dh_recbto),
        "numero_aleatorio": _numero_aleatorio_nfe(chave_acesso),
        "cfop_xml":         g("ide/CFOP"),
        "natureza_operacao": g("ide/natOp"),
        "tp_emis":           g("ide/tpEmis") or "1",
        "quantidade_total":  "1",
        "unidade_medida":    "UN",
        "forma_pagamento":   forma_pagamento, 
        "jur_ini":           jur_ini,
        "jur_fim":           jur_fim,
        "duplicatas":        _extrair_duplicatas(inf, NS_CTE, valor_total),
        "itens":             [{
            "seq": "1", "cfop": g("ide/CFOP"), "quantidade": "1", "unidade": "UN", 
            "valor": valor_total, "descricao": "Frete", "ncm": "", 
            "vIPI": str(v_ipi), "vICMS": str(v_icms), "vPIS": str(v_pis), "vCOFINS": str(v_cofins)
        }],
    }


# ─── NFS-e ───────────────────────────────────────────────────────────────────

def _num(valor: str) -> float:
    try:
        return float(valor) if valor not in (None, "") else 0.0
    except ValueError:
        return 0.0


def parsear_nfse_nacional(xml_bytes: bytes) -> dict:
    """NFS-e do padrão nacional (namespace http://www.sped.fazenda.gov.br/nfse, layout 1.0x).

    Estrutura: NFSe/infNFSe (dados gerados pela Sefin: nNFSe, emit, valores com vISSQN/vLiq) e
    NFSe/infNFSe/DPS/infDPS (declaração do prestador: dhEmi, toma, serv, valores/vServPrest/vServ, trib).
    Retenções: entram em "retencoes" os valores retidos explícitos (vRetIRRF, vRetCSLL, vRetCP), o ISS quando
    tpRetISSQN = 2 ou 3 (retido pelo tomador ou intermediário) e PIS/COFINS quando o líquido mostra que foram
    retidos. O tpRetPisCofins NÃO serve para isso: nos 1019 XMLs de jul/26 o código 1 teve 33 notas com
    PIS/COFINS retido e 8 sem; o 3, 22 com e 30 sem; em branco, 23 com. O critério é o vLiq:
    vServ - descontos - vLiq = demais retenções + vPis + vCofins (± R$ 0,02).
    "retencoes_conferem" = vServ - descontos - vLiq bate com a soma de "retencoes" (False quando o XML é
    incoerente, ex.: retenção declarada e vLiq = vServ).
    """
    raiz = etree.fromstring(xml_bytes)
    N = NS_NFSE_NAC

    inf = raiz if raiz.tag == f"{{{N}}}infNFSe" else raiz.find(f".//{{{N}}}infNFSe")
    if inf is None:
        raise ValueError("XML inválido: NFS-e do padrão nacional sem o grupo infNFSe.")
    dps = inf.find(f".//{{{N}}}infDPS")

    def g(el, caminho: str) -> str:
        if el is None:
            return ""
        alvo = el.find("/".join(f"{{{N}}}{p}" for p in caminho.split("/")))
        return alvo.text.strip() if alvo is not None and alvo.text else ""

    numero_nf = g(inf, "nNFSe")
    data_em = g(dps, "dhEmi") or g(inf, "dhProc")
    v_serv = g(dps, "valores/vServPrest/vServ")
    v_liq = g(inf, "valores/vLiq")

    chave_acesso = re.sub(r"\D", "", inf.get("Id", ""))

    cnpj_emit = re.sub(r"\D", "", g(inf, "emit/CNPJ") or g(inf, "emit/CPF")
                       or g(dps, "prest/CNPJ") or g(dps, "prest/CPF"))
    cnpj_toma = re.sub(r"\D", "", g(dps, "toma/CNPJ") or g(dps, "toma/CPF"))

    if not numero_nf:
        raise ValueError("XML inválido: O XML não possui a tag com o Número da NFS-e (nNFSe).")
    if not data_em:
        raise ValueError("XML inválido: O XML não possui a Data de Emissão da NFS-e (dhEmi).")
    if not v_serv:
        raise ValueError("XML inválido: O Valor do Serviço (vServ) não foi encontrado neste XML de NFS-e.")
    data_em_formatada = _ts_para_date_sap(data_em)
    if not data_em_formatada:
        raise ValueError("XML inválido: A Data de Emissão da NFS-e possui um formato não reconhecido.")

    # ISS
    tp_ret_iss = g(dps, "valores/trib/tribMun/tpRetISSQN")
    v_iss = g(inf, "valores/vISSQN")
    # Tributos federais (grupo tribFed da DPS)
    v_ret_irrf = g(dps, "valores/trib/tribFed/vRetIRRF")
    v_ret_csll = g(dps, "valores/trib/tribFed/vRetCSLL")
    v_ret_cp = g(dps, "valores/trib/tribFed/vRetCP")
    tp_ret_pis_cofins = g(dps, "valores/trib/tribFed/piscofins/tpRetPisCofins")
    v_pis = g(dps, "valores/trib/tribFed/piscofins/vPis")
    v_cofins = g(dps, "valores/trib/tribFed/piscofins/vCofins")

    retencoes = {}
    if tp_ret_iss in ("2", "3") and _num(v_iss) > 0:
        retencoes["vISSRet"] = v_iss
    for chave, valor in (("vIRRF", v_ret_irrf), ("vRetCSLL", v_ret_csll), ("vRetPrev", v_ret_cp)):
        if _num(valor) > 0:
            retencoes[chave] = valor
    v_desc_incond = g(dps, "valores/vDescCondIncond/vDescIncond")
    v_desc_cond = g(dps, "valores/vDescCondIncond/vDescCond")
    descontos = round(_num(v_desc_incond) + _num(v_desc_cond), 2)
    # vLiq = vServ - descontos - retenções
    diferenca_liquido = round(_num(v_serv) - _num(v_liq) - descontos, 2) if v_liq else 0.0
    soma_ret = round(sum(_num(v) for v in retencoes.values()), 2)
    v_pis_cofins = round(_num(v_pis) + _num(v_cofins), 2)
    pis_cofins_retido = v_pis_cofins > 0 and abs(diferenca_liquido - soma_ret - v_pis_cofins) < 0.02
    if pis_cofins_retido:
        if _num(v_pis) > 0:
            retencoes["vRetPIS"] = v_pis
        if _num(v_cofins) > 0:
            retencoes["vRetCOFINS"] = v_cofins
    retencoes_conferem = abs(diferenca_liquido - round(sum(_num(v) for v in retencoes.values()), 2)) < 0.02

    # Vencimento: a NFS-e nacional não tem grupo de cobrança; alguns prestadores escrevem no texto livre.
    duplicatas = []
    texto_livre = " ".join(t for t in (g(dps, "serv/infoCompl/xInfComp"), g(dps, "serv/cServ/xDescServ")) if t)
    venc = re.findall(r"venc(?:imento)?\.?\s*(?:em)?\s*:?\s*(\d{2})/(\d{2})/(\d{4})", texto_livre, re.IGNORECASE)
    if len(venc) == 1:
        dd, mm, aaaa = venc[0]
        duplicatas = [{"nDup": "1", "dVenc": f"{aaaa}-{mm}-{dd}", "vDup": _num(v_liq or v_serv)}]

    descricao = g(dps, "serv/cServ/xDescServ")
    return {
        "tipo":              "NFSE",
        "layout_nfse":       "NACIONAL",
        "br_nf_type":        "SE",
        "numero_nf":         numero_nf,
        "serie":             "",                       # NFS-e não tem série; a série abaixo é a da DPS
        "serie_dps":         g(dps, "serie"),
        "numero_dps":        g(dps, "nDPS"),
        "data_emissao_raw":  data_em[:10],
        "data_emissao":      data_em_formatada,
        "data_competencia":  g(dps, "dCompet"),
        "data_vencimento_raw": duplicatas[0]["dVenc"] if duplicatas else "",
        "valor_total":       v_serv,                   # valor do serviço (bruto)
        "valor_liquido_fatura": v_liq,                 # valor líquido da NFS-e (após retenções/deduções)
        "retencoes":         retencoes,
        "iss": {"tp_ret": tp_ret_iss, "trib": g(dps, "valores/trib/tribMun/tribISSQN"),
                "valor": v_iss, "base": g(inf, "valores/vBC"), "aliquota": g(inf, "valores/pAliqAplic")},
        "retencoes_conferem": retencoes_conferem,
        "diferenca_liquido": diferenca_liquido,       # vServ - vLiq - descontos
        "descontos": {"incondicionado": v_desc_incond, "condicionado": v_desc_cond},
        "pis_cofins_retido": pis_cofins_retido,
        "tp_ret_pis_cofins": tp_ret_pis_cofins,       # informativo: os emissores preenchem de forma inconsistente
        "pis_cofins": {"cst": g(dps, "valores/trib/tribFed/piscofins/CST"), "vPis": v_pis, "vCofins": v_cofins},
        "cnpj_emitente":     cnpj_emit,                # pode ser CPF (11 dígitos), ex.: cartório
        "cnpj_destinatario": cnpj_toma,
        "uf_emitente":       g(inf, "emit/enderNac/UF"),
        "municipio_emitente": g(inf, "emit/enderNac/cMun"),
        "municipio_prestacao": g(dps, "serv/locPrest/cLocPrestacao"),
        "municipio_incidencia": g(inf, "cLocIncid"),
        "codigo_trib_nacional": g(dps, "serv/cServ/cTribNac"),
        "codigo_trib_municipal": g(dps, "serv/cServ/cTribMun"),
        "codigo_nbs":        g(dps, "serv/cServ/cNBS"),
        "chave_acesso":      chave_acesso,
        "ambiente_gerador":  g(inf, "ambGer"),
        "tipo_inscricao":    "",
        "protocolo":         "",
        "data_protocolo":    _ts_para_date_sap(g(inf, "dhProc")),   # "Data procmto." do documento fiscal SE
        "hora_protocolo":    _extrair_hora_sap(g(inf, "dhProc")) if g(inf, "dhProc") else None,
        "numero_aleatorio":  chave_acesso[40:49] if len(chave_acesso) == 50 else "",
        "cfop_xml":          "",
        "natureza_operacao": "SERVICO",
        "forma_pagamento":   "",
        "duplicatas":        duplicatas,
        "itens": [{"seq": "1", "codigo": g(dps, "serv/cServ/cTribNac") or "SERV",
                   "descricao": (descricao or "Serviços Prestados")[:120], "quantidade": "1", "unidade": "UN",
                   "valor": v_serv, "preco_unitario": v_serv, "ncm": "",
                   "vIPI": "0", "vICMS": "0", "vPIS": "0", "vCOFINS": "0"}],
    }


def parsear_nfse(xml_bytes: bytes) -> dict:
    raiz = etree.fromstring(xml_bytes)
    if NS_NFSE_NAC in (raiz.nsmap.get(None) or "") or raiz.find(f".//{{{NS_NFSE_NAC}}}infNFSe") is not None:
        return parsear_nfse_nacional(xml_bytes)

    def _texto(*tags):
        for tag in tags:
            el = raiz.find(f".//{tag}")
            if el is not None and el.text:
                return el.text.strip()
        return ""

    numero_raw = _texto("Numero", "NumeroNFSe", "NumeroDaNfse", "nNFSe", "nNF")
    data_em    = _texto("DataEmissao", "DataHoraEmissao", "DataEmissaoNfse", "dhEmi")
    valor      = _texto("ValorServicos", "ValorTotalServicos", "Valor", "vServ")
    data_venc  = _texto("DataVencimento", "Vencimento")
    
    cnpj       = re.sub(r"\D", "", _texto("Cnpj", "CnpjPrestador", "CNPJ"))
    cnpj_dest  = re.sub(r"\D", "", _texto("CnpjTomador", "CpfCnpjTomador"))

    chave_acesso = re.sub(r"\D", "", _texto("chNFSe", "ChaveAcesso", "chave"))
    
    if not chave_acesso:
        for el in raiz.iter():
            id_attr = el.get("Id", "")
            if id_attr and len(re.sub(r"\D", "", id_attr)) == 50:
                chave_acesso = re.sub(r"\D", "", id_attr)
                break

    ambiente_gerador = ""
    tipo_inscricao = ""
    
    if len(chave_acesso) == 50:
        ambiente_gerador = chave_acesso[7]      
        tipo_inscricao   = chave_acesso[8]      
        cnpj_chave       = chave_acesso[9:23]   
        
        if not cnpj:
            cnpj = cnpj_chave

    if not numero_raw:
        raise ValueError("XML inválido: O XML não possui a tag com o Número da NFS-e.")
    if not data_em:
        raise ValueError("XML inválido: O XML não possui a tag com a Data de Emissão da NFS-e.")
    if not valor:
        raise ValueError("XML inválido: O Valor do Serviço não foi encontrado neste XML de NFS-e.")

    data_em_formatada = _ts_para_date_sap(data_em)
    if not data_em_formatada:
        raise ValueError("XML inválido: A Data de Emissão enviada possui um formato não reconhecido.")
        
    data_emissao_raw = data_em[:10]

    import datetime as dt
    numero_nf = numero_raw
    for ano in range(dt.date.today().year, 2019, -1):
        if numero_raw.startswith(str(ano)) and len(numero_raw) > len(str(ano)):
            numero_nf = str(int(numero_raw[4:]))    
            break

    # Para NFS-e raramente temos o array padrão <dup>, então a tesouraria faz o net due date single
    return {
        "tipo":              "NFSE",
        "br_nf_type":        "SE",
        "numero_nf":         numero_nf,
        "serie":             "",
        "data_emissao_raw":  data_emissao_raw,
        "data_emissao":      data_em_formatada,
        "data_vencimento_raw": data_venc,
        "valor_total":       valor,
        "cnpj_emitente":     cnpj,
        "cnpj_destinatario": cnpj_dest, 
        "uf_emitente":       "",
        "chave_acesso":      chave_acesso,
        "ambiente_gerador":  ambiente_gerador, 
        "tipo_inscricao":    tipo_inscricao,   
        "protocolo":         "",
        "data_protocolo":    None,
        "hora_protocolo":    None,
        "numero_aleatorio":  "",
        "cfop_xml":          "",
        "natureza_operacao": "SERVICO",
        "forma_pagamento":   "", 
        "duplicatas":        [{"nDup": "1", "dVenc": data_venc, "vDup": float(valor)}] if data_venc else [],
        "itens": [{"seq": "1", "codigo": "SERV", "descricao": "Serviços Prestados", "quantidade": "1", "unidade": "UN", "valor": valor, "preco_unitario": valor, "ncm": "", "vIPI":"0", "vICMS":"0", "vPIS":"0", "vCOFINS":"0"}],
    }


# ─── dispatcher ──────────────────────────────────────────────────────────────

def parsear(xml_bytes: bytes) -> dict:
    xml_bytes_limpo = _limpar_xml_sujo(xml_bytes)
    tipo = detectar_tipo(xml_bytes_limpo)
    if tipo == "NFE":
        return parsear_nfe(xml_bytes_limpo)
    if tipo == "CTE":
        return parsear_cte(xml_bytes_limpo)
    if tipo == "NFSE":
        return parsear_nfse(xml_bytes_limpo)
    raise ValueError(f"Tipo desconhecido: {tipo}")