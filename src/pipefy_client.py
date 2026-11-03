import json
import logging
import re
import requests
from urllib.parse import urlsplit

from config import PIPEFY_TOKEN, PIPEFY_API_URL, PIPEFY_FIELDS as PF

log = logging.getLogger(__name__)

HEADERS = {
    "Authorization": f"Bearer {PIPEFY_TOKEN}",
    "Content-Type":  "application/json",
}

ID_CAMPO_DETALHES_ERRO = "detalhes_do_erro"  
ID_CAMPO_NUMERO_SAP    = "n_documento_sap"    
ID_CAMPO_SCORE         = "score" 
ID_CAMPO_AUDITORIA     = "auditoria_fiscal" # Novo campo solicitado
ID_CAMPO_HISTORICO     = "hist_rico_de_erros"  # Histórico de Erros: só acumula, nunca é apagado
MAX_LINHAS_HISTORICO   = 200

_QUERY_CARD = """
query GetCard($id: ID!) {
  card(id: $id) {
    id
    title
    labels { name }
    attachments { url path } 
    parent_relations {
      cards { title }
    }
    fields {
      field { id }
      value
      array_value
    }
  }
}
"""

def buscar_card(card_id: str) -> dict:
    resp = requests.post(PIPEFY_API_URL, json={"query": _QUERY_CARD, "variables": {"id": card_id}}, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if "errors" in data:
        raise RuntimeError(f"Erro Pipefy GraphQL: {data['errors']}")

    card = data["data"]["card"]

    campos_raw = {}
    for f in card.get("fields", []):
        fid = f["field"]["id"]
        val = f.get("value") or ""
        if f.get("array_value"):
            val = f["array_value"][0] if f["array_value"] else val
        campos_raw[fid] = {"value": val}

    def v(field_key: str) -> str:
        fid = PF.get(field_key, "")
        return campos_raw.get(fid, {}).get("value", "") or ""

    xml_urls = _extrair_xml_urls(card) 
    pedido_sap = _limpar_pedido(v("numero_pedido_sap"))
    documentos_existentes = v("n_documento_sap")

    val_bloqueio = campos_raw.get("subir_bloqueado_pagamento", {}).get("value", "") or v("subir_bloqueado_pagamento")
    subir_bloqueado = str(val_bloqueio).strip().upper() == "SIM"

    rotulos_do_card = [lb.get("name", "").upper() for lb in card.get("labels", [])]

    # Todas as operações indicadas pelas etiquetas, em QUALQUER posição (antes valia só a última).
    operacoes = set()
    for rotulo in rotulos_do_card:
        if "ADIANTAMENTO" in rotulo: operacoes.add("adiantamento")
        elif "CARTÃO" in rotulo or "CARTAO" in rotulo: operacoes.add("cartoes")
        elif "AGENCIAMENTO" in rotulo: operacoes.add("agenciamento")
        elif "COMPRA SITE" in rotulo: operacoes.add("compra_site")
        elif "FRETE" in rotulo: operacoes.add("cte")
        elif "MATERIA PRIMA" in rotulo or "MATÉRIA PRIMA" in rotulo: operacoes.add("materia_prima")

    # tipo_operacao (valor único) continua existindo para quem precisa de um só valor (texto do
    # lançamento, fase de destino no main.py). É escolhido por prioridade fixa, não pela ordem das
    # etiquetas. As REGRAS (matéria-prima, bloqueio, Fase 1, cartões) usam o conjunto `operacoes`.
    tipo_operacao = "normal"
    for candidato in ("cartoes", "cte", "materia_prima", "adiantamento", "agenciamento", "compra_site"):
        if candidato in operacoes:
            tipo_operacao = candidato
            break

    # Bloqueio de pagamento: basta qualquer operação que exige bloqueio.
    if operacoes & {"adiantamento", "cartoes", "agenciamento", "compra_site", "cte"}:
        subir_bloqueado = True

    titulo_pai = ""
    relacoes = card.get("parent_relations", [])
    if relacoes and relacoes[0].get("cards"):
        titulo_pai = relacoes[0]["cards"][0].get("title", "")

    texto_sap = ""
    if "cartoes" in operacoes and titulo_pai:
        texto_sap = titulo_pai[:50]

    return {
        "card_id":                   card_id,
        "titulo":                    card.get("title", ""),
        "xml_urls":                  xml_urls, 
        "documentos_existentes":     documentos_existentes, 
        "numero_pedido_sap":         pedido_sap,
        "tipo_operacao":             tipo_operacao,
        "operacoes":                 sorted(operacoes),
        "texto":                     texto_sap,
        "subir_bloqueado_pagamento": subir_bloqueado
    }

def _extrair_xml_urls(card: dict) -> list:
    urls_encontradas = []
    
    url_pattern = re.compile(r'(https?://[^\s",]+)')
    
    def _adicionar_url(url: str, filename: str):
        if not url: return
        u_low = url.lower()
        f_low = filename.lower()
        
        if any(ext in u_low or ext in f_low for ext in [".pdf", ".png", ".jpg", ".jpeg", ".zip", ".rar", ".txt"]):
            return
            
        if f_low.endswith((".xml", ".cte", ".nfe")) or ".xml" in u_low or ".cte" in u_low or ".nfe" in u_low:
            match = url_pattern.search(url)
            if match:
                urls_encontradas.append(match.group(1))

    for att in card.get("attachments", []):
        _adicionar_url(att.get("url", ""), att.get("path", ""))
            
    for f in card.get("fields", []):
        valores = f.get("array_value") or []
        for val in valores:
            if isinstance(val, str):
                _adicionar_url(val, val)
                
        val_str = f.get("value")
        if isinstance(val_str, str) and ("http" in val_str):
            _adicionar_url(val_str, val_str)
                    
    # O mesmo arquivo pode vir em mais de uma URL (anexo do card e valor do campo), cada uma com uma assinatura.
    # Em 09/10 só uma delas abria (as outras davam 401). Fica uma URL por arquivo; as demais são alternativas
    # que o baixar_xml tenta se a primeira não abrir (antes o mesmo XML era baixado 2-3 vezes e o erro das
    # cópias impedia o card de mudar de fase).
    por_arquivo = {}
    for u in urls_encontradas:
        lista = por_arquivo.setdefault(_chave_arquivo(u), [])
        if u not in lista:
            lista.append(u)
    for chave, lista in por_arquivo.items():
        _ALTERNATIVAS_XML[chave] = lista
    return [lista[0] for lista in por_arquivo.values()]


_ALTERNATIVAS_XML: dict = {}


def _chave_arquivo(url: str) -> str:
    """Identifica o arquivo independente da assinatura: caminho sem host/query e sem o prefixo do storage."""
    caminho = urlsplit(url).path if "://" in url else url.split("?")[0]
    caminho = caminho.lstrip("/")
    for prefixo in ("storage/v1/signed/",):
        if caminho.startswith(prefixo):
            caminho = caminho[len(prefixo):]
    return caminho

def _limpar_pedido(valor: str) -> str:
    if not valor: return ""
    nums_encontrados = re.findall(r"\d{5,10}", str(valor))
    pedidos_limpos = [n.lstrip("0") or n for n in nums_encontrados]
    return ", ".join(pedidos_limpos)

def baixar_xml(url: str) -> bytes:
    if not url: raise ValueError("XML da NF não encontrado nos anexos do card.")
    
    if url.startswith("uploads/"):
        url = "https://app.pipefy.com/" + url
        
    # URL assinada do storage do Pipefy (.../storage/v1/signed/...?expires_on=...&signature=...): a assinatura já
    # autoriza o download. Em 09/10 (a partir das 08:03) o storage passou a responder 500 a todo download com o
    # cabeçalho Authorization + Content-Type JSON. Tenta primeiro sem cabeçalho; se não der, tenta como antes.
    # Em 09/10 o card trouxe 3 URLs do mesmo XML: com cabeçalho todas davam 500; sem cabeçalho, 2 davam 401 e
    # 1 abria. Tenta todas as URLs do arquivo sem cabeçalho; por último, a primeira como antes (com cabeçalho).
    candidatas = [url] + [u for u in _ALTERNATIVAS_XML.get(_chave_arquivo(url), []) if u != url]
    candidatas = [("https://app.pipefy.com/" + u) if u.startswith("uploads/") else u for u in candidatas]
    tentativas = [(u, None) for u in candidatas if "signature=" in u or "/storage/v1/signed/" in u]
    tentativas.append((url, HEADERS))
    resp = primeira_falha = None
    for u, headers in tentativas:
        resp = requests.get(u, headers=headers, timeout=30)
        if resp.ok:
            break
        primeira_falha = primeira_falha or resp
        log.warning("Download do XML: HTTP %s (%s cabeçalho) em %s", resp.status_code,
                    "com" if headers else "sem", _chave_arquivo(u))
    if not resp.ok:
        (primeira_falha or resp).raise_for_status()
    content = resp.content
    
    if not content.strip().startswith(b"<"): 
        raise ValueError("O arquivo baixado não é um XML válido. Verifique se o anexo está correto e não é um PDF ou imagem.")
    return content

def _gql_str(texto) -> str:
    """Texto como string GraphQL válida. O escape JSON é compatível com o do GraphQL (\\n, \\", \\\\, ...).
    Antes só as aspas eram escapadas: valor com QUEBRA DE LINHA (ex.: o texto da Auditoria Fiscal)
    deixava a mutation inválida e o campo não era gravado (erro engolido no try/except de quem chama)."""
    return json.dumps(str(texto), ensure_ascii=False)

def comentar_card(card_id: str, texto: str):
    query = "mutation { createComment(input: {card_id: %s, text: %s}) { comment { id } } }" % (_gql_str(card_id), _gql_str(texto))
    requests.post(PIPEFY_API_URL, json={"query": query}, headers=HEADERS, timeout=15)

def atualizar_campo(card_id: str, field_id: str, valor: str):
    query = f'mutation {{ updateCardField(input: {{card_id: {_gql_str(card_id)}, field_id: {_gql_str(field_id)}, new_value: {_gql_str(valor)}}}) {{ success }} }}'
    resp = requests.post(PIPEFY_API_URL, json={"query": query}, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data: raise RuntimeError(f"Erro de mutação no campo {field_id}: {data['errors']}")

def linha_historico(erro: str, quando=None) -> str:
    """'dd/mm/aaaa HH:MM | <erro numa linha só>' (horário de Brasília)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    quando = quando or datetime.now(ZoneInfo("America/Sao_Paulo"))
    texto = re.sub(r"\s+", " ", str(erro or "")).strip()
    return f"{quando.strftime('%d/%m/%Y %H:%M')} | {texto[:600]}"


def juntar_historico(atual: str, novas: list) -> str:
    """Acrescenta linhas ao histórico, sem repetir linha idêntica, em ordem de data, até MAX_LINHAS_HISTORICO."""
    linhas = [l for l in str(atual or "").splitlines() if l.strip()]
    for l in novas:
        if l and l not in linhas:
            linhas.append(l)
    def chave(l):
        m = re.match(r"(\d{2})/(\d{2})/(\d{4}) (\d{2}:\d{2})", l)
        return (m.group(3), m.group(2), m.group(1), m.group(4)) if m else ("9999",)
    linhas.sort(key=chave)
    return "\n".join(linhas[-MAX_LINHAS_HISTORICO:])


def ler_campo(card_id: str, field_id: str) -> str:
    q = "query($id: ID!) { card(id: $id) { fields { field { id } value } } }"
    resp = requests.post(PIPEFY_API_URL, json={"query": q, "variables": {"id": str(card_id)}}, headers=HEADERS, timeout=15).json()
    if "errors" in resp:
        raise RuntimeError(resp["errors"])
    return next((f.get("value") or "" for f in resp["data"]["card"]["fields"] if f["field"]["id"] == field_id), "")


def acrescentar_historico_erros(card_id: str, erros: list):
    """Grava os erros no campo 'Histórico de Erros' (o 'Detalhes do Erro' guarda só o erro atual e é limpo
    quando o card é lançado). Nunca derruba o fluxo da automação."""
    if not erros:
        return
    try:
        atual = ler_campo(card_id, ID_CAMPO_HISTORICO)
        atualizar_campo(card_id, ID_CAMPO_HISTORICO, juntar_historico(atual, [linha_historico(e) for e in erros]))
    except Exception as e:
        log.warning("Não consegui gravar o Histórico de Erros do card %s: %s", card_id, e)


PREFIXO_AJUSTE_CP = "✅ Contas a Pagar:"
_AJUSTES_CP = (("Valor das parcelas ajustado automaticamente", "valor das parcelas"),
               ("Vencimento ajustado automaticamente", "vencimento"),
               ("Adiantamento(s) do card compensado(s) automaticamente", "adiantamento do card"))


def com_linha_de_ajuste(texto_auditoria: str) -> str:
    """Põe na PRIMEIRA linha da Auditoria Fiscal um resumo dos ajustes automáticos de parcelas
    (ex.: "✅ Contas a Pagar: valor das parcelas ajustado automaticamente (NF 50118)").
    O filtro "contém" dos relatórios do Pipefy só enxerga o começo de um texto longo, e o texto
    da Fase 3 fica no fim. Sem ajuste, ou se a linha já existe, devolve o texto como veio."""
    texto = str(texto_auditoria or "")
    if not texto or texto.lstrip().startswith(PREFIXO_AJUSTE_CP):
        return texto
    nf_atual, achados = None, []
    for linha in texto.splitlines():
        m = re.match(r"\s*\[NF ([^\]]+)\]", linha)
        if m:
            nf_atual = m.group(1).strip()
        for frase, rotulo in _AJUSTES_CP:
            if frase in linha:
                item = f"{rotulo} ajustado automaticamente" + (f" (NF {nf_atual})" if nf_atual else "")
                if item not in achados:
                    achados.append(item)
    if not achados:
        return texto
    return f"{PREFIXO_AJUSTE_CP} " + "; ".join(achados) + "\n" + texto.lstrip("\n")


def campo_por_rotulo(card_id: str, trecho_rotulo: str) -> str:
    """ID do campo do card cujo rótulo contém o trecho (sem diferenciar maiúsculas). '' se não achar."""
    q = "query($id: ID!) { card(id: $id) { fields { field { id label } } } }"
    resp = requests.post(PIPEFY_API_URL, json={"query": q, "variables": {"id": str(card_id)}}, headers=HEADERS, timeout=15).json()
    if "errors" in resp:
        raise RuntimeError(resp["errors"])
    trecho = trecho_rotulo.lower()
    return next((f["field"]["id"] for f in resp["data"]["card"]["fields"]
                 if trecho in str(f["field"].get("label") or "").lower()), "")


def mover_card_verificado(card_id: str, phase_id: str) -> str:
    """Move o card e devolve '' se o Pipefy aceitou, ou o motivo da recusa (ex.: campo obrigatório da fase)."""
    query = "mutation MoveCard($c: ID!, $p: ID!) { moveCardToPhase(input: {card_id: $c, destination_phase_id: $p}) { card { id current_phase { id } } } }"
    resp = requests.post(PIPEFY_API_URL, json={"query": query, "variables": {"c": card_id, "p": phase_id}}, headers=HEADERS, timeout=15)
    try:
        data = resp.json()
    except Exception:
        return f"HTTP {resp.status_code}"
    if data.get("errors"):
        return "; ".join(str(e.get("message") or e) for e in data["errors"])[:300]
    fase = (((data.get("data") or {}).get("moveCardToPhase") or {}).get("card") or {}).get("current_phase") or {}
    if str(fase.get("id") or "") != str(phase_id):
        return f"o card continua na fase {fase.get('id') or '?'}"
    return ""


def mover_card(card_id: str, phase_id: str):
    query = "mutation MoveCard($c: ID!, $p: ID!) { moveCardToPhase(input: {card_id: $c, destination_phase_id: $p}) { card { id } } }"
    requests.post(PIPEFY_API_URL, json={"query": query, "variables": {"c": card_id, "p": phase_id}}, headers=HEADERS, timeout=15)

def registrar_lote_pipefy(card_id: str, sucessos: list, erros: list, max_score: int, tipos_docs: list, relatorio_pop: str = ""):
    str_sucessos = " | ".join(sucessos) if sucessos else ""
    
    if str_sucessos:
        try: atualizar_campo(card_id, ID_CAMPO_NUMERO_SAP, str_sucessos)
        except Exception: pass
        try: atualizar_campo(card_id, "n_mero_do_lan_amento_2", str_sucessos)
        except Exception: pass
        try: atualizar_campo(card_id, "card_lan_ado_no_sap", "Sim")
        except Exception: pass

    if sucessos and max_score >= 0:
        try: atualizar_campo(card_id, ID_CAMPO_SCORE, str(max_score))
        except Exception: pass

    str_erros = " | ".join(erros) if erros else ""
    try: atualizar_campo(card_id, ID_CAMPO_DETALHES_ERRO, str_erros[:255])
    except Exception: pass
    acrescentar_historico_erros(card_id, erros)
    
    teve_falha = "SIM" if erros else "NÂO"
    try: atualizar_campo(card_id, "ocorreu_alguma_falha_no_lan_amento", teve_falha)
    except Exception: pass
    
    try: atualizar_campo(card_id, "ser_descontinuado", "NÃO")
    except Exception: pass

    if tipos_docs:
        mapa_tipos = {"NFE": "NFE (Modelo 55)", "CTE": "CTE (Modelo 57)", "NFSE": "Serviço NFS"}
        tipos_formatados = list(set([mapa_tipos.get(t, "Outros") for t in tipos_docs]))
        try: atualizar_campo(card_id, "qual_o_tipo_de_documento_fiscal", ", ".join(tipos_formatados))
        except Exception: pass
        
    if relatorio_pop:
        try: atualizar_campo(card_id, ID_CAMPO_AUDITORIA, com_linha_de_ajuste(relatorio_pop))
        except Exception: pass

    msg_comentario = "🤖 Resumo do Lançamento em Lote\n\n"
    if sucessos:
        msg_comentario += "SUCESSOS:\n" + "\n".join([f"✅ {s}" for s in sucessos]) + "\n\n"
    if erros:
        msg_comentario += "FALHAS:\n" + "\n".join([f"❌ {e}" for e in erros]) + "\n\n"
        msg_comentario += "Ação necessária: Corrija os erros apontados e mova o card para tentar novamente.\n\n"
    
    if sucessos and max_score >= 0:
        msg_comentario += f"📊 Score Máximo de Risco: {max_score}/100\n\n"
        
    if relatorio_pop:
        msg_comentario += f"{relatorio_pop}"

    comentar_card(card_id, msg_comentario)

def registrar_sucesso(card_id: str, resultado: dict, tipo_doc: str):
    doc  = resultado.get("supplier_invoice", "")
    ano  = resultado.get("fiscal_year", "")
    post = "Registrado ✅" if resultado.get("posted") else "Criado (registrar manualmente) ⚠️"
    score = resultado.get("risk_score")
    relatorio_pop = resultado.get("relatorio_pop", "")

    if doc:
        try: atualizar_campo(card_id, ID_CAMPO_NUMERO_SAP, f"{doc}/{ano}")
        except Exception: pass
        try: atualizar_campo(card_id, "n_mero_do_lan_amento_2", f"{doc}/{ano}")
        except Exception: pass
        try: atualizar_campo(card_id, "card_lan_ado_no_sap", "Sim")
        except Exception: pass

    if score is not None:
        try: atualizar_campo(card_id, ID_CAMPO_SCORE, str(score))
        except Exception: pass
        
    if relatorio_pop:
        try: atualizar_campo(card_id, ID_CAMPO_AUDITORIA, com_linha_de_ajuste(relatorio_pop))
        except Exception: pass

    try: atualizar_campo(card_id, "ocorreu_alguma_falha_no_lan_amento", "NÂO")
    except Exception: pass
    try: atualizar_campo(card_id, "ser_descontinuado", "NÃO")
    except Exception: pass

    mapa_tipos = {"NFE": "NFE (Modelo 55)", "CTE": "CTE (Modelo 57)", "NFSE": "Serviço NFS"}
    tipo_formatado = mapa_tipos.get(tipo_doc, "Outros")
    try: atualizar_campo(card_id, "qual_o_tipo_de_documento_fiscal", tipo_formatado)
    except Exception: pass
    try: atualizar_campo(card_id, ID_CAMPO_DETALHES_ERRO, "")
    except Exception: pass

    msg_sucesso = f"🤖 Lançamento automático SAP\nDocumento: {doc} / {ano}\nStatus: {post}\nScore de Risco: {score}/100"
    
    if relatorio_pop:
        msg_sucesso += f"\n\n{relatorio_pop}"

    comentar_card(card_id, msg_sucesso)

def registrar_erro(card_id: str, erro: str, responsavel: str, tipo_doc: str = "", is_critical: bool = True, apenas_marcar_erro: bool = False):
    if not is_critical:
        comentar_card(card_id, f"⚠️ Aviso: O processamento não prosseguiu.\n\nMotivo:\n{erro}")
        return

    # apenas_marcar_erro: usado pro caso de "XML da NF ausente no anexo" -- preenche
    # "Detalhes do Erro" (campo que o relatorio/dashboard do Pipefy usa pra contar o
    # card no Total Bruto e classificar como "Falta de XML no Card", ja excluido da
    # Base Elegivel/Taxa de Sucesso do dashboard) mas NAO marca
    # "ocorreu_alguma_falha_no_lan_amento"/"ser_descontinuado" nem usa o comentario
    # de falha critica, porque isso nao e' uma falha da automacao pra ser corrigida
    # por Compras/Financeiro -- e' o solicitante que precisa anexar o XML.
    if not apenas_marcar_erro:
        try: atualizar_campo(card_id, "ocorreu_alguma_falha_no_lan_amento", "SIM")
        except Exception: pass
        try: atualizar_campo(card_id, "ser_descontinuado", "NÃO")
        except Exception: pass

    erro_formatado = f"[{responsavel}] {erro}"
    try: atualizar_campo(card_id, ID_CAMPO_DETALHES_ERRO, erro_formatado[:255])
    except Exception: pass
    acrescentar_historico_erros(card_id, [erro_formatado])
    
    if tipo_doc:
        mapa_tipos = {"NFE": "NFE (Modelo 55)", "CTE": "CTE (Modelo 57)", "NFSE": "Serviço NFS"}
        tipo_formatado = mapa_tipos.get(tipo_doc, "Outros")
        try: atualizar_campo(card_id, "qual_o_tipo_de_documento_fiscal", tipo_formatado)
        except Exception: pass

    if apenas_marcar_erro:
        comentar_card(card_id, f"⚠️ Aviso: O processamento não prosseguiu.\n\nMotivo:\n{erro}")
    else:
        comentar_card(card_id, f"❌ Falha no lançamento automático\nResponsável sugerido: {responsavel}\n\nDetalhes:\n{erro}\n\nAção necessária: Corrija os dados apontados.")