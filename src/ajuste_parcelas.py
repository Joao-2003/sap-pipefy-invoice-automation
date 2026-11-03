"""
ajuste_parcelas.py -- Fase 3 (Contas a Pagar): deixa as parcelas do fornecedor no SAP
IGUAIS às duplicatas do XML da NF (quantidade, valores e vencimentos).

Mesmo método que o Contas a Pagar já usa manualmente (documentos KZ da analista do Contas a Pagar) e que o
colega validou em QAS para o Contas a Receber (teste_compensador.py), em 2 passos:

  Passo A -- Journal Entry Post SÍNCRONO (JournalEntryCreateRequestConfirmation_In):
             documento tipo KZ com
               * 1 linha de CONTRAPARTIDA no fornecedor = total das parcelas atuais (débito)
               * N parcelas novas no fornecedor = duplicatas do XML (crédito), cada uma com
                 data-base = vencimento do XML e 0 dias (não puxa condição do cadastro)
             -> devolve o número do documento na hora
  Passo B -- Journal Entry Clearing ASSÍNCRONO + WS-RM (JournalEntryBulkClearingRequest_In):
             compensa as parcelas atuais contra a contrapartida do passo A.

Resultado: parcelas atuais compensadas; só as parcelas novas (iguais ao XML) ficam em aberto.

Bloqueio de pagamento das parcelas novas:
  * herdam o bloqueio que a fatura tem (ex.: "A" de adiantamento/cartão, "R"), e
  * a parcela já paga por adiantamento (NF com 1 parcela a mais que o SAP, a 1ª vencendo até
    a emissão -- mesma regra da Fase 1) nasce bloqueada (BLOQUEIO_PARCELA_ANTECIPADA) para o
    Contas a Pagar compensar contra o adiantamento.

ESTA FERRAMENTA É PARA TESTE INDIVIDUAL NO SERVIDOR. Ainda não é chamada pela automação.

Uso (no servidor, em /var/www/nfautomation):
  venv/bin/python ajuste_parcelas.py --fatura 5100553451 --ano 2026 --xml /caminho/NF.xml
      -> SIMULAÇÃO (padrão): lê o SAP, monta e valida os 2 XMLs, mostra o plano. Não envia nada.
  venv/bin/python ajuste_parcelas.py --fatura 5100553451 --ano 2026 --xml /caminho/NF.xml --enviar
      -> envia de verdade (pede confirmação digitando 's').
  Opcional: --mostrar-xml (imprime os XMLs), --empresa BR01 (padrão BR01).

Teste offline (sem SAP): --offline arquivo.json com {"fatura": {...}, "parcelas": [linhas da CDS]}.

Só compensar uma parcela já paga por adiantamento com o adiantamento (razão especial A) em aberto:
  venv/bin/python ajuste_parcelas.py --fatura 5100553451 --ano 2026 --compensar-parcela 1505432702/2026/001 [--enviar]
      -> acha o adiantamento A em aberto de mesmo valor (ou --adiantamento DOC/ANO/ITEM) e compensa os dois.
"""
import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

import requests
from lxml import etree

# ======================================================================
# CONFIGURAÇÃO
# ======================================================================
TIPO_DOC_NOVAS = "RE"            # parcelas novas como RE, igual às da fatura (Contas a Pagar, 08/10/2026;
                                 # até 08/10 eram KZ -- convertidas com --converter)
TIPOS_DOC_AJUSTE = ("KZ", "RE")  # tipos que a automação já usou nos documentos de ajuste
TIPO_DOC_COMPENSACAO = "KZ"      # documento de compensação (analista do Contas a Pagar mantém KZ)
BLOQUEIO_PARCELA_ANTECIPADA = "A"
CRIADO_POR = "COMM_USER"        # CreatedByUser (obrigatório no schema, máx. 12)
TEXTO_CABECALHO = "Ajuste parcelas NF {nf}"   # máx. 25
# Estorno pelo próprio serviço de Post (cabeçalho com ReversalReason + ReversalReferenceDocument, sem itens).
# 02 = estorno com data de lançamento diferente da original (o 01 exige a mesma data).
MOTIVO_ESTORNO = "02"
# Se a compensação das parcelas antigas não for confirmada, estorna o KZ novo (sem parcelas em dobro).
ESTORNAR_SE_NAO_COMPENSAR = True
TOLERANCIA_IGUAL = Decimal("0.005")
POLL_TENTATIVAS = 10
POLL_INTERVALO_S = 6
# ======================================================================

CENTAVO = Decimal("0.01")
_DIR = os.path.dirname(os.path.abspath(__file__))
WSDL_POST = os.path.join(_DIR, "apis_sap", "JOURNALENTRYCREATEREQUESTCONFI.wsdl")
WSDL_CLEAR = os.path.join(_DIR, "apis_sap", "JOURNALENTRYBULKCLEARINGREQUES.wsdl")
ACT_POST = ("http://sap.com/xi/SAPSCORE/SFIN/JournalEntryCreateRequestConfirmation_In/"
            "JournalEntryCreateRequestConfirmation_InRequest")
ACT_CLEAR = ("http://sap.com/xi/SAPSCORE/SFIN/JournalEntryBulkClearingRequest_In/"
             "JournalEntryBulkClearingRequest_InRequest")
WSDL_CHANGE = os.path.join(_DIR, "apis_sap", "JOURNALENTRYBULKCHANGEREQUEST_.wsdl")
ACT_CHANGE = ("http://sap.com/xi/SAPSCORE/SFIN/JournalEntryBulkChangeRequest_In/"
              "JournalEntryBulkChangeRequest_InRequest")

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
WSA_NS = "http://www.w3.org/2005/08/addressing"
WSRM_NS = "http://docs.oasis-open.org/ws-rx/wsrm/200702"
SFIN_NS = "http://sap.com/xi/SAPSCORE/SFIN"
XS_NS = "http://www.w3.org/2001/XMLSchema"
NSMAP = {"soap-env": SOAP_NS, "wsa": WSA_NS, "wsrm": WSRM_NS}
ANONYMOUS = f"{WSA_NS}/anonymous"


# ----------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------
def _dec(v) -> Decimal:
    return Decimal(str(v)).quantize(CENTAVO, ROUND_HALF_UP)


def _data_odata(valor):
    if not valor:
        return None
    m = re.search(r"/Date\((-?\d+)", str(valor))
    if not m:
        return None
    return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date()


def _agora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sub(pai, tag, texto=None, **attrs):
    el = etree.SubElement(pai, tag, **attrs)
    if texto is not None:
        el.text = str(texto)
    return el


def _endpoint(wsdl_path, padrao):
    """Sempre usa o host de SAP_BASE_URL (o .env manda); do WSDL aproveita só o caminho."""
    from urllib.parse import urlsplit
    try:
        r = etree.parse(wsdl_path).getroot()
        for a in r.iter("{http://schemas.xmlsoap.org/wsdl/soap/}address"):
            if a.get("location"):
                base = urlsplit(padrao)
                return f"{base.scheme}://{base.netloc}{urlsplit(a.get('location')).path}"
    except Exception:
        pass
    return padrao


_schemas = {}


def _schema_do_wsdl(wsdl_path):
    """Monta o XMLSchema do WSDL. Com vários schemas (ex.: campos YY1_ no Change), grava cada um num
    arquivo temporário e liga os xs:import por schemaLocation."""
    import tempfile
    schemas = list(etree.parse(wsdl_path).getroot().iter(f"{{{XS_NS}}}schema"))
    if len(schemas) == 1:
        return etree.XMLSchema(etree.fromstring(etree.tostring(schemas[0])))
    tmp = tempfile.mkdtemp(prefix="ajparc_xsd_")
    arq = {sc.get("targetNamespace"): os.path.join(tmp, f"s{i}.xsd") for i, sc in enumerate(schemas)}
    principal = None
    for sc in schemas:
        sc = etree.fromstring(etree.tostring(sc))
        tns = sc.get("targetNamespace")
        ja = set()
        for imp in sc.findall(f"{{{XS_NS}}}import"):
            ns = imp.get("namespace")
            if ns in arq:
                imp.set("schemaLocation", arq[ns]); ja.add(ns)
        # importa também namespaces usados sem xs:import declarado
        usados = {v for v in sc.nsmap.values() if v in arq and v != tns} - ja
        for ns in usados:
            imp = etree.Element(f"{{{XS_NS}}}import", namespace=ns, schemaLocation=arq[ns])
            sc.insert(0, imp)
        etree.ElementTree(sc).write(arq[tns], xml_declaration=True, encoding="UTF-8")
        if tns == SFIN_NS:
            principal = arq[tns]
    return etree.XMLSchema(etree.parse(principal or arq[schemas[0].get("targetNamespace")]))


def validar(envelope, wsdl_path, rotulo):
    """Valida o elemento de negócio contra o schema do WSDL. Devolve lista de erros (vazia = ok)."""
    if wsdl_path not in _schemas:
        sch = None
        if os.path.exists(wsdl_path):
            sch = _schema_do_wsdl(wsdl_path)
        _schemas[wsdl_path] = sch
    sch = _schemas[wsdl_path]
    if sch is None:
        return [f"{rotulo}: WSDL não encontrado em apis_sap/ (sem validação)"]
    payload = envelope.find(f"{{{SOAP_NS}}}Body")[0]
    doc = etree.fromstring(etree.tostring(payload))
    if sch.validate(doc):
        return []
    return [f"{rotulo}: linha {e.line}: {e.message}" for e in sch.error_log]


# ----------------------------------------------------------------------
# Leitura do SAP (OData) -- reaproveita autenticação/URLs do sap_client
# ----------------------------------------------------------------------
class LeitorSAP:
    def __init__(self):
        import sap_client as sc
        self.sc = sc
        self.s = requests.Session()

    def _get(self, url):
        r = self.s.get(url, headers={**self.sc._auth_headers(), "Accept": "application/json"},
                       **self.sc._auth_kwargs(), timeout=30)
        r.raise_for_status()
        return r.json().get("d", {})

    def fatura(self, fatura, ano):
        d = self._get(f"{self.sc.BASE}/A_SupplierInvoice(SupplierInvoice='{fatura}',FiscalYear='{ano}')"
                      f"?$select=SupplierInvoice,FiscalYear,CompanyCode,InvoicingParty,InvoiceGrossAmount,"
                      f"PaymentBlockingReason,BusinessPlace,DocumentDate,SupplierInvoiceIDByInvcgParty&$format=json")
        return d

    def documento_contabil(self, fatura, ano, empresa):
        awkey = f"{fatura}{ano}"
        for _ in range(3):
            d = self._get(f"{self.sc.BASE_DEPARA}/YY1_FATURA_DEPARA?$filter=OriginalReferenceDocument eq '{awkey}'"
                          f" and CompanyCode eq '{empresa}'&$format=json")
            res = d.get("results", [])
            if res:
                return res[0].get("AccountingDocument")
            time.sleep(2)
        return None

    def linhas(self, documento, ano, empresa):
        d = self._get(f"{self.sc.BASE_SUPPLIER_ITEMS}/YY1_SUPPLIER_LINE_ITEMS?$filter=AccountingDocument eq "
                      f"'{documento}' and CompanyCode eq '{empresa}' and FiscalYear eq '{ano}'&$format=json")
        return d.get("results", [])

    def linhas_compensadas_por(self, doc_comp, fornecedor, empresa):
        """Itens do fornecedor compensados pelo documento doc_comp (qualquer ano)."""
        d = self._get(f"{self.sc.BASE_SUPPLIER_ITEMS}/YY1_SUPPLIER_LINE_ITEMS?$filter=ClearingJournalEntry eq "
                      f"'{doc_comp}' and Supplier eq '{fornecedor}' and CompanyCode eq '{empresa}'&$format=json")
        return d.get("results", [])

    def fatura_do_documento(self, documento, ano, empresa):
        """(fatura, ano) da fatura cujo lançamento contábil é `documento` (YY1_FATURA_DEPARA ao contrário)."""
        d = self._get(f"{self.sc.BASE_DEPARA}/YY1_FATURA_DEPARA?$filter=AccountingDocument eq '{documento}'"
                      f" and CompanyCode eq '{empresa}' and FiscalYear eq '{ano}'&$format=json")
        for r in d.get("results", []):
            ref = str(r.get("OriginalReferenceDocument") or "")
            if len(ref) >= 14 and ref[:10].isdigit():
                return ref[:10], ref[10:14]
        return None, None

    def adiantamentos_abertos(self, fornecedor, empresa):
        """Adiantamentos pagos (razão especial A) ainda não compensados do fornecedor."""
        d = self._get(f"{self.sc.BASE_SUPPLIER_ITEMS}/YY1_SUPPLIER_LINE_ITEMS?$filter=Supplier eq '{fornecedor}'"
                      f" and CompanyCode eq '{empresa}' and SpecialGLCode eq 'A' and ClearingJournalEntry eq ''"
                      f"&$format=json")
        return d.get("results", [])


def _campos_compensacao(linha: dict):
    """Descobre os nomes dos campos de compensação na CDS (dependem da fonte da view)."""
    # ClearingJournalEntry é o atual; ClearingAccountingDocument está obsoleto na I_OperationalAcctgDocItem
    doc = next((k for k in ("ClearingJournalEntry", "ClearingAccountingDocument") if k in linha), None)
    dt = "ClearingDate" if "ClearingDate" in linha else None
    return doc, dt


# ----------------------------------------------------------------------
# Plano
# ----------------------------------------------------------------------
def _card_id_das_parcelas(atuais: list):
    """Lê o ID do card do texto do item (a automação grava 'NF x, ID <card>, ...' no lançamento)."""
    for l in atuais:
        m = re.search(r"\bID\s*(\d+)", str(l.get("DocumentItemText") or ""))
        if m:
            return m.group(1)
    return None


def _prefixo_card(plano):
    return f"ID {plano['card_id']} " if plano.get("card_id") else ""


def duplicatas_com_retencao(dados_nf: dict, total_sap) -> tuple:
    """NF com impostos retidos (retTrib): as duplicatas somam o líquido e o SAP grava o total da NF.
    Procedimento do Contas a Pagar (analista do Contas a Pagar, NF 175102 -> KZ 1509201813): parcelas iguais ao XML e a
    retenção somada na ÚLTIMA parcela, mantendo o total. Só quando SAP - XML = soma das retenções
    (± R$ 0,05). Devolve (duplicatas a usar, valor da retenção somada ou 0)."""
    dups = list(dados_nf.get("duplicatas") or [])
    try:
        ret = sum((_dec(v) for v in (dados_nf.get("retencoes") or {}).values()), Decimal("0"))
        total_xml = sum((_dec(d["vDup"]) for d in dups), Decimal("0"))
        total_sap = _dec(total_sap)
    except Exception:
        return dups, Decimal("0")
    if not dups or ret <= 0 or abs((total_sap - total_xml) - ret) > Decimal("0.05"):
        return dups, Decimal("0")
    ultima = dict(dups[-1])
    ultima["vDup"] = str((_dec(ultima["vDup"]) + (total_sap - total_xml)).quantize(CENTAVO))
    return dups[:-1] + [ultima], (total_sap - total_xml).quantize(CENTAVO)


def montar_plano(fatura: dict, parcelas_sap: list, dados_nf: dict, card_id=None) -> dict:
    """Compara SAP x XML e devolve o plano de ajuste (ou motivo para não ajustar)."""
    # parcela = linha do fornecedor COM vencimento (a CDS traz o fornecedor também em linhas que não são
    # contas a pagar, sem vencimento -- NF 02152)
    atuais = sorted([l for l in parcelas_sap if str(l.get("Supplier", "")).strip() and l.get("NetDueDate")
                     and not str(l.get("SpecialGLCode") or "").strip()],
                    key=lambda l: str(l.get("AccountingDocumentItem", "")))
    dups = dados_nf.get("duplicatas", [])
    plano = {"acao": None, "motivo": "", "atuais": atuais, "novas": [],
             "card_id": card_id or _card_id_das_parcelas(atuais)}

    if not atuais:
        plano.update(acao="nada", motivo="Nenhuma parcela de fornecedor encontrada no lançamento.")
        return plano
    if not dups:
        plano.update(acao="nada", motivo="O XML não tem duplicatas: não há referência para ajustar.")
        return plano

    campo_doc, _ = _campos_compensacao(atuais[0])
    if campo_doc:
        ja = [l for l in atuais if str(l.get(campo_doc) or "").strip()]
        if ja:
            plano.update(acao="nada", motivo=f"{len(ja)} parcela(s) já compensada(s) (pagas ou ajustadas). Não mexo.")
            return plano

    total_sap = sum((-_dec(l["AmountInCompanyCodeCurrency"]) for l in atuais), Decimal("0"))
    dups, plano["retencao"] = duplicatas_com_retencao(dados_nf, total_sap)
    total_xml = sum((_dec(d["vDup"]) for d in dups), Decimal("0"))
    if abs(total_sap - total_xml) > TOLERANCIA_IGUAL:
        plano.update(acao="nada", motivo=f"Total das parcelas no SAP (R$ {total_sap}) diferente do total das "
                                        f"duplicatas (R$ {total_xml}). Ajuste manual.")
        return plano

    iguais = len(atuais) == len(dups) and all(
        abs(-_dec(l["AmountInCompanyCodeCurrency"]) - _dec(d["vDup"])) <= TOLERANCIA_IGUAL
        and _data_odata(l.get("NetDueDate")) == date.fromisoformat(d["dVenc"][:10])
        for l, d in zip(atuais, dups)
    )
    if iguais:
        plano.update(acao="nada", motivo="As parcelas do SAP já estão iguais às duplicatas do XML.")
        return plano

    # Mesma quantidade e mesmos valores: só a data difere -> altera o vencimento do próprio item
    # (Change), sem criar documento novo nem compensar.
    so_data = len(atuais) == len(dups) and all(
        abs(-_dec(l["AmountInCompanyCodeCurrency"]) - _dec(d["vDup"])) <= TOLERANCIA_IGUAL
        for l, d in zip(atuais, dups))
    if so_data:
        plano["datas"] = [(l, date.fromisoformat(d["dVenc"][:10])) for l, d in zip(atuais, dups)
                          if _data_odata(l.get("NetDueDate")) != date.fromisoformat(d["dVenc"][:10])]
        plano.update(acao="alterar_data", total=total_sap, total_contra=total_sap)
        return plano

    bloqueio_fatura = str(fatura.get("PaymentBlockingReason") or "").strip()
    antecipada = False
    try:
        emissao = date.fromisoformat(str(dados_nf.get("data_emissao_raw", ""))[:10])
        vence_na_emissao = date.fromisoformat(dups[0]["dVenc"][:10]) <= emissao + timedelta(days=1)
        antecipada = len(dups) == len(atuais) + 1 and vence_na_emissao
        # NF 44383: adiantamento DENTRO das parcelas (XML e SAP com a mesma quantidade). Só vira antecipada
        # se houver adiantamento A em aberto com o mesmo valor (marcar_antecipada_mesma_qtd).
        plano["candidata_antecipada"] = len(dups) == len(atuais) > 1 and vence_na_emissao
    except ValueError:
        pass

    for i, d in enumerate(dups):
        bloq = bloqueio_fatura
        if i == 0 and antecipada:
            bloq = BLOQUEIO_PARCELA_ANTECIPADA
        plano["novas"].append({
            "valor": _dec(d["vDup"]),
            "data_base": date.fromisoformat(d["dVenc"][:10]),
            "bloqueio": bloq,
            "texto": f"{_prefixo_card(plano)}Parcela {i + 1}/{len(dups)} NF {dados_nf.get('numero_nf', '')}",
            "antecipada": i == 0 and antecipada,
        })
    plano.update(acao="ajustar", total=total_sap, total_contra=total_sap)
    return plano


def vincular_adiantamento(plano, candidatos: list):
    """Na parcela antecipada, acha o adiantamento (razão especial A, em aberto) com o mesmo valor.
    Com mais de um candidato, só aceita o que tiver o ID do card no texto/atribuição."""
    if plano.get("acao") != "ajustar" or not plano["novas"] or not plano["novas"][0]["antecipada"]:
        return
    if plano.get("adiantamento"):  # já escolhido por marcar_antecipada_mesma_qtd
        return
    valor = plano["novas"][0]["valor"]
    mesmos = [c for c in candidatos if abs(abs(_dec(c["AmountInCompanyCodeCurrency"])) - valor) <= TOLERANCIA_IGUAL]
    card = plano.get("card_id")
    com_card = [c for c in mesmos if card and card in (str(c.get("DocumentItemText") or "")
                                                       + " " + str(c.get("AssignmentReference") or ""))]
    escolhido = com_card[0] if len(com_card) == 1 else (mesmos[0] if len(mesmos) == 1 else None)
    plano["adiantamento"] = escolhido
    plano["adiantamento_candidatos"] = len(mesmos)


def marcar_antecipada_mesma_qtd(plano, candidatos: list) -> bool:
    """Adiantamento dentro das próprias parcelas (NF 44383): o XML e o SAP têm a mesma quantidade de parcelas
    e a 1ª duplicata vence até a emissão+1. A 1ª parcela só é tratada como paga por adiantamento (bloqueio A
    e compensação) se houver adiantamento A em aberto com o MESMO valor, identificado com segurança.
    Sem isso o plano continua um reparcelamento normal, igual à NF (pedido da analista do Contas a Pagar, 14/10/2026)."""
    if plano.get("acao") != "ajustar" or not plano.get("candidata_antecipada") or not plano["novas"]:
        return False
    p1 = plano["novas"][0]
    adto, n = escolher_adiantamento(p1["valor"], candidatos, plano.get("card_id"))
    plano["adiantamento_candidatos"] = n
    if not adto:
        return False
    p1.update(antecipada=True, bloqueio=BLOQUEIO_PARCELA_ANTECIPADA)
    plano["adiantamento"] = adto
    return True


def usar_compensacao_do_adiantamento(plano):
    """Variante 'ideal' da analista do Contas a Pagar: não cria a parcela antecipada; compensa o adiantamento junto.
    Post: contrapartida = total - adiantamento + parcelas 2..n. Clearing: antigas + adiantamento + contrapartida."""
    adto = plano.get("adiantamento")
    if not adto:
        return False
    valor = plano["novas"][0]["valor"]
    plano["novas"] = plano["novas"][1:]
    plano["total_contra"] = plano["total"] - valor
    plano["modo_adto"] = True
    return True


# ----------------------------------------------------------------------
# Passo A: Post síncrono
# ----------------------------------------------------------------------
def _envelope_vazio():
    env = etree.Element(f"{{{SOAP_NS}}}Envelope", nsmap={**NSMAP, "sfin": SFIN_NS})
    etree.SubElement(env, f"{{{SOAP_NS}}}Header")
    body = etree.SubElement(env, f"{{{SOAP_NS}}}Body")
    return env, body


def _valor_e_lado(pai, valor: Decimal, lado: str):
    """No Post o SINAL define D/C (positivo = débito S, negativo = crédito H)."""
    assinado = valor if lado == "S" else -valor
    _sub(pai, "AmountInTransactionCurrency", f"{assinado:.2f}", currencyCode="BRL")
    _sub(pai, "DebitCreditCode", lado)


def montar_post(plano, fatura, dados_nf, empresa, message_id):
    nf = dados_nf.get("numero_nf", "")
    fornecedor = fatura["InvoicingParty"]
    hoje = date.today().isoformat()
    data_doc = _data_odata(fatura.get("DocumentDate")) or date.today()
    business_place = str(fatura.get("BusinessPlace") or "").strip()

    env, body = _envelope_vazio()
    root = _sub(body, f"{{{SFIN_NS}}}JournalEntryBulkCreateRequest")
    mh = _sub(root, "MessageHeader")
    _sub(mh, "ID", message_id)
    _sub(mh, "CreationDateTime", _agora())
    req = _sub(root, "JournalEntryCreateRequest")
    mh2 = _sub(req, "MessageHeader")
    _sub(mh2, "ID", f"{message_id}_1")
    _sub(mh2, "CreationDateTime", _agora())

    je = _sub(req, "JournalEntry")
    _sub(je, "OriginalReferenceDocumentType", "BKPFF")
    _sub(je, "BusinessTransactionType", "RFBU")
    _sub(je, "AccountingDocumentType", TIPO_DOC_NOVAS)
    _sub(je, "DocumentReferenceID", f"NF {nf}"[:16])
    _sub(je, "DocumentHeaderText", TEXTO_CABECALHO.format(nf=nf)[:25])
    _sub(je, "CreatedByUser", CRIADO_POR[:12])
    _sub(je, "CompanyCode", empresa)
    _sub(je, "DocumentDate", data_doc.isoformat())
    _sub(je, "PostingDate", hoje)

    # Contrapartida (débito no fornecedor = total das parcelas atuais) -- será compensada no passo B
    cp = _sub(je, "CreditorItem")
    _sub(cp, "ReferenceDocumentItem", "1")
    _sub(cp, "Creditor", fornecedor)
    _valor_e_lado(cp, plano["total_contra"], "S")
    _sub(cp, "DocumentItemText", f"{_prefixo_card(plano)}Contrapartida ajuste NF {nf}"[:50])
    if business_place:
        _sub(cp, "BusinessPlace", business_place)

    # Parcelas novas (crédito no fornecedor), iguais às duplicatas do XML
    for i, p in enumerate(plano["novas"], start=2):
        it = _sub(je, "CreditorItem")
        _sub(it, "ReferenceDocumentItem", str(i))
        _sub(it, "Creditor", fornecedor)
        _valor_e_lado(it, p["valor"], "H")
        _sub(it, "DocumentItemText", p["texto"][:50])
        cdt = _sub(it, "CashDiscountTerms")
        _sub(cdt, "DueCalculationBaseDate", p["data_base"].isoformat())
        _sub(cdt, "NetPaymentDays", "P0D")
        if p["bloqueio"]:
            pdt = _sub(it, "PaymentDetails")
            _sub(pdt, "PaymentBlockingReason", p["bloqueio"])
        if business_place:
            _sub(it, "BusinessPlace", business_place)
    return env


def _local(root, nome):
    return [el for el in root.iter() if isinstance(el.tag, str) and etree.QName(el).localname == nome]


def enviar_post(s, env, endpoint, out=print):
    resp = s.post(endpoint, data=etree.tostring(env, xml_declaration=True, encoding="UTF-8"),
                  headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ACT_POST}, timeout=120)
    try:
        root = etree.fromstring(resp.content)
    except etree.XMLSyntaxError:
        raise RuntimeError(f"Post: resposta não-XML (HTTP {resp.status_code}): {resp.text[:1500]}")
    fault = root.find(f".//{{{SOAP_NS}}}Fault")
    if fault is not None:
        raise RuntimeError(f"Post: SOAP Fault (HTTP {resp.status_code}): "
                           f"{fault.findtext('faultstring') or etree.tostring(fault, encoding='unicode')}")
    notas = [n.text for n in _local(root, "Note") if n.text]
    for n in notas:
        out(f"   SAP: {n}")
    docs = [d.text for d in _local(root, "AccountingDocument") if d.text]
    anos = [a.text for a in _local(root, "FiscalYear") if a.text]
    doc = docs[0] if docs else ""
    if not doc or set(doc) == {"0"}:
        out(etree.tostring(root, pretty_print=True, encoding="unicode")[:4000])
        raise RuntimeError("Post NÃO gerou documento. Nada foi compensado.")
    return doc, (anos[0] if anos else str(date.today().year))


# ----------------------------------------------------------------------
# Passo B: Clearing assíncrono (WS-RM)
# ----------------------------------------------------------------------
def montar_clearing(plano, fatura, dados_nf, empresa, ano_atuais, message_id, doc_novo, ano_novo, item_contra):
    nf = dados_nf.get("numero_nf", "")
    hoje = date.today().isoformat()
    env, body = _envelope_vazio()
    root = _sub(body, f"{{{SFIN_NS}}}JournalEntryBulkClearingRequest")
    mh = _sub(root, "MessageHeader")
    _sub(mh, "ID", message_id)
    _sub(mh, "CreationDateTime", _agora())
    req = _sub(root, "JournalEntryClearingRequest")
    mh2 = _sub(req, "MessageHeader")
    _sub(mh2, "ID", f"{message_id}_1")
    _sub(mh2, "CreationDateTime", _agora())
    je = _sub(req, "JournalEntry")
    _sub(je, "CompanyCode", empresa)
    _sub(je, "AccountingDocumentType", TIPO_DOC_COMPENSACAO)
    _sub(je, "DocumentDate", hoje)
    _sub(je, "PostingDate", hoje)
    _sub(je, "CurrencyCode", "BRL")
    _sub(je, "DocumentHeaderText", TEXTO_CABECALHO.format(nf=nf)[:25])

    itens = [(l["AccountingDocument"], ano_atuais, str(l["AccountingDocumentItem"]).zfill(3)) for l in plano["atuais"]]
    if plano.get("modo_adto"):
        a = plano["adiantamento"]
        itens.append((a["AccountingDocument"], str(a["FiscalYear"]), str(a["AccountingDocumentItem"]).zfill(3)))
    itens.append((doc_novo, ano_novo, str(item_contra).zfill(3)))
    for ref, (docu, ano, item) in enumerate(itens, start=1):
        ap = _sub(je, "APARItems")
        _sub(ap, "ReferenceDocumentItem", str(ref))
        _sub(ap, "CompanyCode", empresa)
        _sub(ap, "AccountType", "K")
        _sub(ap, "APARAccount", fatura["InvoicingParty"])
        _sub(ap, "FiscalYear", ano)
        _sub(ap, "AccountingDocument", docu)
        _sub(ap, "AccountingDocumentItem", item)
    return env


def _post_rm(s, endpoint, env_bytes, action):
    return s.post(endpoint, data=env_bytes, timeout=60,
                  headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": action})


def _checar(resp, etapa):
    root = None
    if resp.content and resp.content.strip():
        try:
            root = etree.fromstring(resp.content)
        except etree.XMLSyntaxError:
            pass
    if root is not None:
        fault = root.find(f".//{{{SOAP_NS}}}Fault")
        if fault is not None:
            raise RuntimeError(f"{etapa}: SOAP Fault (HTTP {resp.status_code}): "
                               f"{fault.findtext('faultstring') or etree.tostring(fault, encoding='unicode')}")
    if resp.status_code >= 400:
        raise RuntimeError(f"{etapa}: HTTP {resp.status_code}: {resp.text[:1000]}")
    return root


def _env_protocolo(endpoint, action, body_el):
    env = etree.Element(f"{{{SOAP_NS}}}Envelope", nsmap=NSMAP)
    hdr = etree.SubElement(env, f"{{{SOAP_NS}}}Header")
    etree.SubElement(hdr, f"{{{WSA_NS}}}To").text = endpoint
    etree.SubElement(hdr, f"{{{WSA_NS}}}Action").text = action
    etree.SubElement(hdr, f"{{{WSA_NS}}}MessageID").text = f"urn:uuid:{uuid.uuid4()}"
    rt = etree.SubElement(hdr, f"{{{WSA_NS}}}ReplyTo")
    etree.SubElement(rt, f"{{{WSA_NS}}}Address").text = ANONYMOUS
    etree.SubElement(env, f"{{{SOAP_NS}}}Body").append(body_el)
    return etree.tostring(env, xml_declaration=True, encoding="UTF-8")


def _injetar_rm(env, endpoint, seq_id, action=ACT_CLEAR):
    hdr = env.find(f"{{{SOAP_NS}}}Header")
    etree.SubElement(hdr, f"{{{WSA_NS}}}To").text = endpoint
    etree.SubElement(hdr, f"{{{WSA_NS}}}Action").text = action
    etree.SubElement(hdr, f"{{{WSA_NS}}}MessageID").text = f"urn:uuid:{uuid.uuid4()}"
    rt = etree.SubElement(hdr, f"{{{WSA_NS}}}ReplyTo")
    etree.SubElement(rt, f"{{{WSA_NS}}}Address").text = ANONYMOUS
    seq = etree.SubElement(hdr, f"{{{WSRM_NS}}}Sequence")
    etree.SubElement(seq, f"{{{WSRM_NS}}}Identifier").text = seq_id
    etree.SubElement(seq, f"{{{WSRM_NS}}}MessageNumber").text = "1"


def enviar_clearing(s, env, endpoint):
    enviar_wsrm(s, env, endpoint, ACT_CLEAR, "Compensação")


def enviar_wsrm(s, env, endpoint, action, rotulo, out=print):
    """Uma sequência WS-RM por mensagem: CreateSequence -> mensagem -> TerminateSequence."""
    a_cs = f"{WSRM_NS}/CreateSequence"
    cs = etree.Element(f"{{{WSRM_NS}}}CreateSequence")
    acks = etree.SubElement(cs, f"{{{WSRM_NS}}}AcksTo")
    etree.SubElement(acks, f"{{{WSA_NS}}}Address").text = ANONYMOUS
    root = _checar(_post_rm(s, endpoint, _env_protocolo(endpoint, a_cs, cs), a_cs), "CreateSequence")
    ident = None if root is None else root.find(".//wsrm:CreateSequenceResponse/wsrm:Identifier", NSMAP)
    if ident is None or not ident.text:
        raise RuntimeError("CreateSequence não devolveu wsrm:Identifier")
    seq_id = ident.text
    try:
        _injetar_rm(env, endpoint, seq_id, action)
        resp = _post_rm(s, endpoint, etree.tostring(env, xml_declaration=True, encoding="UTF-8"), action)
        _checar(resp, rotulo)
        out(f"   {rotulo}: aceito pelo SAP (HTTP {resp.status_code}) -- processamento assíncrono")
    finally:
        a_ts = f"{WSRM_NS}/TerminateSequence"
        ts = etree.Element(f"{{{WSRM_NS}}}TerminateSequence")
        etree.SubElement(ts, f"{{{WSRM_NS}}}Identifier").text = seq_id
        try:
            _post_rm(s, endpoint, _env_protocolo(endpoint, a_ts, ts), a_ts)
        except Exception as ex:
            out(f"   [aviso] TerminateSequence falhou (não crítico): {ex}")


# ----------------------------------------------------------------------
# Só data: Change assíncrono (WS-RM) -- apaga a condição, depois muda a data-base
# ----------------------------------------------------------------------
def montar_change(message_id, empresa, ano, documento, item, bloco, campo, valor):
    """Uma alteração de campo em um item. Valor vazio + FieldValueChangeIsRequested=true = limpar."""
    env, body = _envelope_vazio()
    msg = _sub(body, f"{{{SFIN_NS}}}JournalEntryBulkChangeRequestMessage")
    mh = _sub(msg, "MessageHeader")
    _sub(mh, "ID", message_id)
    _sub(mh, "CreationDateTime", _agora())
    it = _sub(msg, "JournalEntryDebtorCreditorItem")
    mhi = _sub(it, "MessageHeader")
    _sub(mhi, "ID", f"{message_id}_1")
    _sub(mhi, "CreationDateTime", _agora())
    key = _sub(it, "ItemKey")
    _sub(key, "AccountingDocument", documento)
    _sub(key, "CompanyCode", empresa)
    _sub(key, "FiscalYear", ano)
    _sub(key, "AccountingDocumentItemID", str(item).zfill(3))
    chg = _sub(it, bloco)
    _sub(chg, campo, valor)
    _sub(chg, "FieldValueChangeIsRequested", "true")
    return env


def _linha_item(leitor, documento, ano, empresa, item):
    for l in leitor.linhas(documento, ano, empresa):
        if str(l.get("AccountingDocumentItem")).zfill(3) == str(item).zfill(3):
            return l
    return None


def _aguardar(leitor, documento, ano, empresa, item, condicao, descricao, out=print):
    for i in range(1, POLL_TENTATIVAS + 1):
        time.sleep(POLL_INTERVALO_S)
        l = _linha_item(leitor, documento, ano, empresa, item)
        if l is not None and condicao(l):
            return l
        out(f"      tentativa {i}: aguardando {descricao}")
    return None


def executar_alteracao_datas(s, leitor, plano, empresa, ano, documento, endpoint, base_id, out=print):
    for n, (l, alvo) in enumerate(plano["datas"], start=1):
        item = str(l["AccountingDocumentItem"]).zfill(3)
        out(f"\n[item {item}] vencimento {_data_odata(l.get('NetDueDate'))} -> {alvo}")
        atual = _linha_item(leitor, documento, ano, empresa, item) or l
        if str(atual.get("ClearingJournalEntry") or "").strip():
            out("   item já compensado -- pulei."); continue

        if str(atual.get("PaymentTerms") or "").strip():
            mid = f"AJDATA_{base_id}_{n}PT"
            out(f"   1) apagando a condição de pagamento {atual.get('PaymentTerms')} (ID {mid})")
            enviar_wsrm(s, montar_change(mid, empresa, ano, documento, item, "PaymentTermsChange", "PaymentTerms", ""),
                        endpoint, ACT_CHANGE, "Apagar condição", out)
            if not _aguardar(leitor, documento, ano, empresa, item,
                             lambda x: not str(x.get("PaymentTerms") or "").strip(), "condição ficar vazia", out):
                out(f"   Condição não ficou vazia no tempo de espera. Data NÃO alterada. Veja o Message Dashboard (ID {mid}).")
                return False

        antes = _data_odata(atual.get("NetDueDate"))
        base = alvo
        for tentativa in (1, 2):
            mid = f"AJDATA_{base_id}_{n}D{tentativa}"
            out(f"   {tentativa + 1}) data-base -> {base} (ID {mid})")
            enviar_wsrm(s, montar_change(mid, empresa, ano, documento, item, "DueCalculationBaseDateChange",
                                         "DueCalculationBaseDate", base.isoformat()),
                        endpoint, ACT_CHANGE, "Alterar data-base", out)
            ref = antes
            nova = _aguardar(leitor, documento, ano, empresa, item,
                             lambda x: _data_odata(x.get("NetDueDate")) not in (ref, None), "o vencimento mudar", out)
            if not nova:
                out(f"   Vencimento não mudou no tempo de espera. Veja o Message Dashboard (ID {mid}).")
                return False
            venc = _data_odata(nova.get("NetDueDate"))
            if venc == alvo:
                out(f"   OK: vencimento {venc}.")
                plano.setdefault("ajustados", []).append((item, _data_odata(l.get("NetDueDate")), venc))
                break
            # Sobrou prazo gravado no item (ex.: 30 dias): desconta da data-base e reenvia uma vez
            dif = (venc - alvo).days
            out(f"   vencimento ficou {venc} ({dif:+d} dias de prazo gravado no item)")
            if tentativa == 2:
                out("   Não consegui acertar o vencimento. Ajuste manual deste item.")
                return False
            antes, base = venc, alvo - timedelta(days=dif)
    return True


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def imprimir_plano(plano, fatura, documento, ano):
    print(f"Fornecedor {fatura.get('InvoicingParty')} | lançamento {documento}/{ano} | "
          f"bloqueio da fatura: '{fatura.get('PaymentBlockingReason') or ''}'")
    print("\nParcelas atuais no SAP:")
    for l in plano["atuais"]:
        print(f"   item {l['AccountingDocumentItem']}  venc. {_data_odata(l.get('NetDueDate'))}  "
              f"R$ {-_dec(l['AmountInCompanyCodeCurrency']):>12}  cond. {l.get('PaymentTerms') or '-'}")
    if plano["acao"] == "alterar_data":
        print("\nSó a data difere (mesma quantidade e valores): altero o vencimento do próprio item,")
        print("sem documento novo e sem compensação. A condição de pagamento do item é apagada antes.")
        for l, alvo in plano["datas"]:
            print(f"   item {l['AccountingDocumentItem']}: {_data_odata(l.get('NetDueDate'))} -> {alvo}")
        return
    if plano["acao"] != "ajustar":
        print(f"\n>>> Nada a fazer: {plano['motivo']}")
        return
    print("\nParcelas novas (iguais ao XML):")
    for p in plano["novas"]:
        extra = "  (já paga por adiantamento)" if p["antecipada"] else ""
        print(f"   venc. {p['data_base']}  R$ {p['valor']:>12}  bloqueio '{p['bloqueio']}'{extra}")
    if plano.get("retencao"):
        print(f"\nImpostos retidos na NF: R$ {plano['retencao']} somados na última parcela (total igual ao da NF).")
    adto = plano.get("adiantamento")
    if plano["novas"] and plano["novas"][0]["antecipada"] or plano.get("modo_adto"):
        if adto:
            print(f"\nAdiantamento correspondente: doc {adto['AccountingDocument']}/{adto['FiscalYear']} item "
                  f"{adto['AccountingDocumentItem']}  R$ {abs(_dec(adto['AmountInCompanyCodeCurrency']))}  "
                  f"atrib. '{adto.get('AssignmentReference') or ''}'")
        elif plano.get("adiantamento_candidatos") is not None:
            print(f"\nAdiantamento correspondente: não identificado com segurança "
                  f"({plano['adiantamento_candidatos']} candidato(s) com o mesmo valor). analista do Contas a Pagar confere.")
    if plano.get("modo_adto"):
        print(f"\nMODO COMPENSAR ADIANTAMENTO: a 1ª parcela não é criada; o adiantamento entra na compensação.")
    print(f"\nTotal antigo: R$ {plano['total']} | contrapartida a débito no {TIPO_DOC_NOVAS}: R$ {plano['total_contra']}")


def main():
    ap = argparse.ArgumentParser(description="Ajusta as parcelas do fornecedor no SAP para ficarem iguais ao XML.")
    ap.add_argument("--fatura", required=True, help="nº da fatura SAP (ex.: 5100553451)")
    ap.add_argument("--ano", required=True)
    ap.add_argument("--xml", help="caminho do XML da NF-e (não precisa com --compensar-parcela)")
    ap.add_argument("--empresa", default="BR01")
    ap.add_argument("--card", help="ID do card Pipefy para o texto do item (padrão: lido do lançamento original)")
    ap.add_argument("--enviar", action="store_true", help="envia de verdade (sem isso, só simula)")
    ap.add_argument("--compensar-adiantamento", action="store_true",
                    help="EXPERIMENTAL: compensa o adiantamento junto em vez de criar a 1ª parcela bloqueada")
    ap.add_argument("--mostrar-xml", action="store_true")
    ap.add_argument("--offline", help="JSON com {'fatura':..., 'parcelas':...} para testar sem SAP")
    ap.add_argument("--compensar-parcela", metavar="DOC/ANO/ITEM",
                    help="só compensa essa parcela (já paga por adiantamento) com o adiantamento A em aberto")
    ap.add_argument("--adiantamento", metavar="DOC/ANO/ITEM",
                    help="com --compensar-parcela: força o adiantamento (padrão: o A em aberto de mesmo valor)")
    ap.add_argument("--compensar-itens", nargs="+", metavar="DOC/ANO[/ITEM]",
                    help="só compensa esses itens do fornecedor entre si (precisam somar zero). Ex.: refazer a "
                         "compensação de um reparcelamento que o SAP não processou. DOC/ANO sem item = todos os "
                         "itens do fornecedor em aberto do documento (ex.: lançamento estornado + estorno)")
    ap.add_argument("--estornar", metavar="DOC/ANO",
                    help="estorna um KZ de ajuste lançado pela automação (ex.: 1542040222/2026). Só se nenhum "
                         "item estiver compensado")
    ap.add_argument("--motivo", default=MOTIVO_ESTORNO,
                    help=f"com --estornar: motivo de estorno (padrão {MOTIVO_ESTORNO})")
    ap.add_argument("--referencia", help="com --estornar: chave de referência do lançamento (padrão DOC+EMPRESA+ANO)")
    ap.add_argument("--converter", nargs="+", metavar="DOC/ANO",
                    help="converte as parcelas EM ABERTO de documentos de ajuste KZ para RE (documento RE novo com as "
                         "mesmas parcelas + compensação das parcelas do KZ). Sem --fatura/--xml")
    ap.add_argument("--aceitar-manual", action="store_true",
                    help="com --converter: aceita KZ de reparcelamento feito à mão no app (sem o texto da automação)")
    if "--converter" in sys.argv:  # --fatura/--ano não se aplicam
        for o in ap._actions:
            if o.dest in ("fatura", "ano"):
                o.required = False
    a = ap.parse_args()
    if a.converter:
        return main_converter(a)
    if a.estornar:
        return main_estornar(a)
    if a.compensar_parcela:
        return main_compensar_adiantamento(a)
    if a.compensar_itens:
        return main_compensar_itens(a)
    if not a.xml:
        ap.error("--xml é obrigatório (exceto com --compensar-parcela / --compensar-itens / --estornar)")

    import xml_parser
    dados_nf = xml_parser.parsear_nfe(open(a.xml, "rb").read())

    if a.offline:
        o = json.load(open(a.offline, encoding="utf-8"))
        fatura, parcelas, documento = o["fatura"], o["parcelas"], o.get("documento", "?")
        adiantamentos = o.get("adiantamentos", [])
        leitor = None
    else:
        leitor = LeitorSAP()
        fatura = leitor.fatura(a.fatura, a.ano)
        documento = leitor.documento_contabil(a.fatura, a.ano, a.empresa)
        if not documento:
            sys.exit("Não encontrei o lançamento contábil da fatura (YY1_FATURA_DEPARA).")
        parcelas = leitor.linhas(documento, a.ano, a.empresa)
        adiantamentos = None

    print("=" * 78)
    plano = montar_plano(fatura, parcelas, dados_nf, a.card)
    if plano["acao"] == "ajustar" and plano.get("candidata_antecipada"):
        if adiantamentos is None:
            try:
                adiantamentos = leitor.adiantamentos_abertos(fatura["InvoicingParty"], a.empresa)
            except Exception as e:
                print(f"[aviso] não consegui ler os adiantamentos do fornecedor: {e}")
                adiantamentos = []
        if marcar_antecipada_mesma_qtd(plano, adiantamentos):
            print("1ª duplicata vence na emissão e há adiantamento A em aberto com o mesmo valor: "
                  "1ª parcela tratada como paga por adiantamento.")
        else:
            print(f"1ª duplicata vence na emissão, mas nenhum adiantamento A em aberto com o mesmo valor foi "
                  f"identificado com segurança ({plano.get('adiantamento_candidatos', 0)} candidato(s)): "
                  f"reparcelo igual à NF, sem compensação.")
    if plano["acao"] == "ajustar" and plano["novas"] and plano["novas"][0]["antecipada"]:
        if adiantamentos is None:
            try:
                adiantamentos = leitor.adiantamentos_abertos(fatura["InvoicingParty"], a.empresa)
            except Exception as e:
                print(f"[aviso] não consegui ler os adiantamentos do fornecedor: {e}")
                adiantamentos = []
        vincular_adiantamento(plano, adiantamentos)
        if a.compensar_adiantamento and not usar_compensacao_do_adiantamento(plano):
            sys.exit("--compensar-adiantamento: adiantamento não identificado com segurança. Nada feito.")
    imprimir_plano(plano, fatura, documento, a.ano)
    print("=" * 78)
    if plano["acao"] == "alterar_data":
        return main_alterar_data(a, plano, leitor, documento)
    if plano["acao"] != "ajustar":
        return

    base_id = uuid.uuid4().hex[:10].upper()
    id_post, id_clear = f"AJPARC_{base_id}_P", f"AJPARC_{base_id}_C"
    env_post = montar_post(plano, fatura, dados_nf, a.empresa, id_post)
    env_clear_teste = montar_clearing(plano, fatura, dados_nf, a.empresa, a.ano, id_clear,
                                      "9999999999", str(date.today().year), "001")
    erros = validar(env_post, WSDL_POST, "Post") + validar(env_clear_teste, WSDL_CLEAR, "Compensação")
    if erros:
        print("XML INVÁLIDO contra o WSDL -- nada será enviado:")
        for e in erros:
            print("   -", e)
        sys.exit(1)
    print("XMLs válidos contra os WSDLs.")
    if a.mostrar_xml:
        print("\n[A] Post:\n" + etree.tostring(env_post, pretty_print=True, encoding="unicode"))
        print("[B] Compensação (doc/item da contrapartida preenchidos após o passo A):\n"
              + etree.tostring(env_clear_teste, pretty_print=True, encoding="unicode"))

    sem_campo_comp = _campos_compensacao(plano["atuais"][0])[0] is None
    if sem_campo_comp:
        print("\nATENÇÃO: a CDS YY1_SUPPLIER_LINE_ITEMS não informa compensação. Não consigo saber se as")
        print("parcelas acima ainda estão EM ABERTO (podem já ter sido pagas ou ajustadas à mão).")
        print("Antes de --enviar, confira no app 'Administrar itens fornecedor' que estão em aberto.")
    if not a.enviar:
        print("\nSIMULAÇÃO -- nada foi enviado. Use --enviar para executar.")
        return
    if leitor is None:
        sys.exit("Modo offline não envia.")
    if sem_campo_comp:
        if input("\nDigite ABERTAS para confirmar que conferiu as parcelas em aberto: ").strip().upper() != "ABERTAS":
            sys.exit("Cancelado.")
    if input("\nConfirma o envio ao SAP? [s/n]: ").strip().lower() != "s":
        sys.exit("Cancelado.")

    from config import SAP_USERNAME, SAP_PASSWORD
    from requests.auth import HTTPBasicAuth
    s = requests.Session()
    s.auth = HTTPBasicAuth(SAP_USERNAME, SAP_PASSWORD)
    res = executar_reparcelamento(s, leitor, plano, fatura, dados_nf, a.empresa, a.ano, documento,
                                  ids=(id_post, id_clear))
    if res.get("falha") and res.get("doc_novo"):
        sys.exit(2)


def executar_reparcelamento(s, leitor, plano, fatura, dados_nf, empresa, ano, documento, ids=None, out=print):
    """Passo A (Post KZ com parcelas novas + contrapartida) e passo B (compensação das antigas).
    Devolve {"ok", "doc_novo", "ano_novo", "doc_comp", "problemas", "falha"}. Nunca levanta exceção
    depois do Post: se algo falhar com o KZ já lançado, devolve falha com o aviso de estorno."""
    res = {"ok": False, "doc_novo": None, "ano_novo": None, "doc_comp": None, "problemas": [], "falha": None}
    if ids is None:
        base_id = uuid.uuid4().hex[:10].upper()
        ids = (f"AJPARC_{base_id}_P", f"AJPARC_{base_id}_C")
    id_post, id_clear = ids
    env_post = montar_post(plano, fatura, dados_nf, empresa, id_post)
    erros = validar(env_post, WSDL_POST, "Post")
    if erros:
        res["falha"] = "XML do lançamento inválido contra o WSDL: " + "; ".join(erros)
        out(res["falha"])
        return res
    ep_post = _endpoint(WSDL_POST, leitor.sc.SAP_BASE_URL.rstrip("/") + "/sap/bc/srt/scs_ext/sap/journalentrycreaterequestconfi")
    ep_clear = _endpoint(WSDL_CLEAR, leitor.sc.SAP_BASE_URL.rstrip("/") + "/sap/bc/srt/scs_ext/sap/journalentrybulkclearingreques")

    out(f"\n[A] Criando documento {TIPO_DOC_NOVAS} com as parcelas novas (ID {id_post})...")
    try:
        doc_novo, ano_novo = enviar_post(s, env_post, ep_post, out=out)
    except Exception as e:
        res["falha"] = f"lançamento das parcelas novas recusado pelo SAP: {e}. Nada foi alterado."
        out("   " + res["falha"])
        return res
    res.update(doc_novo=doc_novo, ano_novo=ano_novo)
    out(f"   Documento criado: {doc_novo}/{ano_novo}")
    aviso_estorno = (f"o documento {doc_novo} está lançado e NADA foi compensado: parcelas antigas E novas em "
                     f"aberto. Estorne o {doc_novo} ou compense manualmente.")
    try:
        res = _reparcelamento_passo_b(s, leitor, plano, fatura, dados_nf, empresa, ano, documento,
                                      doc_novo, ano_novo, id_clear, ep_clear, aviso_estorno, res, out)
    except Exception as e:
        res["falha"] = f"erro inesperado depois do lançamento ({e}); verifique: {aviso_estorno}"
        out("\n   ATENÇÃO: " + res["falha"])
    if not res.get("ok") and ESTORNAR_SE_NAO_COMPENSAR:
        _estornar_kz_nao_compensado(s, leitor, empresa, res, ep_post, out)
    return res


def _estornar_kz_nao_compensado(s, leitor, empresa, res, ep_post, out):
    """O SAP não compensou as parcelas antigas (ex.: NF 359599, "informação inconsistente IRF" -- cadastro do
    fornecedor). Para não deixar parcelas em dobro, estorna o KZ novo: tudo volta a ser como na fatura e
    Contas a Pagar ajusta manualmente (abrindo chamado de cadastro, se for o caso), como se a automação não
    tivesse feito nada. Se o estorno for recusado porque a compensação entrou atrasada, conta como sucesso."""
    doc_novo, ano_novo = res["doc_novo"], res["ano_novo"]
    motivo = res["falha"]
    out(f"   Motivo: {motivo}")

    def contra_compensada():
        for l in leitor.linhas(doc_novo, ano_novo, empresa):
            if str(l.get("Supplier") or "").strip() and _dec(l["AmountInCompanyCodeCurrency"]) > 0:
                return str(l.get("ClearingJournalEntry") or "").strip()
        return ""

    out(f"\n[C] Compensação não confirmada: estornando o {doc_novo}/{ano_novo} para não deixar parcelas em dobro...")
    try:
        env = montar_estorno(doc_novo, ano_novo, empresa, f"AJEST_{uuid.uuid4().hex[:10].upper()}")
        doc_est, _ = enviar_post(s, env, ep_post, out=out)
    except Exception as e:
        comp = contra_compensada()
        if comp and comp != doc_novo:
            res.update(ok=True, doc_comp=comp, falha=None)
            out(f"   A compensação entrou atrasada (documento {comp}); nada a estornar.")
            return
        res["falha"] = (f"{motivo} | O estorno automático também falhou ({e}): estorne o {doc_novo} "
                        f"(ajuste_parcelas.py --estornar {doc_novo}/{ano_novo}) ou compense manualmente.")
        out("   ATENÇÃO: " + res["falha"])
        return
    res["estorno"] = doc_est
    res["falha"] = (f"o SAP não compensou as parcelas antigas. O lançamento das parcelas novas ({doc_novo}) foi "
                    f"estornado automaticamente (estorno {doc_est}): as parcelas continuam como na fatura, sem nada "
                    f"em dobro. Contas a Pagar: ajustar manualmente; se o SAP der erro de IRF ao compensar, abrir "
                    f"chamado para o Cadastro ajustar o fornecedor antes.")
    out(f"   Estorno {doc_est} lançado: parcelas voltam a ser só as da fatura. Ajuste manual pela Contas a Pagar.")


def _reparcelamento_passo_b(s, leitor, plano, fatura, dados_nf, empresa, ano, documento,
                            doc_novo, ano_novo, id_clear, ep_clear, aviso_estorno, res, out):

    # Localiza o item da contrapartida no documento novo (linha do fornecedor com o total, positiva)
    item_contra = None
    linhas_novas = []
    for _ in range(POLL_TENTATIVAS):
        linhas_novas = [l for l in leitor.linhas(doc_novo, ano_novo, empresa) if str(l.get("Supplier", "")).strip()]
        for l in linhas_novas:
            if abs(_dec(l["AmountInCompanyCodeCurrency"]) - plano["total_contra"]) <= TOLERANCIA_IGUAL:
                item_contra = str(l["AccountingDocumentItem"]).zfill(3)
        if item_contra:
            break
        time.sleep(POLL_INTERVALO_S)
    if not item_contra:
        res["falha"] = f"não localizei a contrapartida; {aviso_estorno}"
        out("\n   ATENÇÃO: " + res["falha"])
        return res
    out(f"   Contrapartida: item {item_contra}")
    res["problemas"] = conferir_parcelas_novas(plano, linhas_novas, item_contra, out=out)

    env_clear = montar_clearing(plano, fatura, dados_nf, empresa, ano, id_clear, doc_novo, ano_novo, item_contra)
    erros = validar(env_clear, WSDL_CLEAR, "Compensação")
    if erros:
        res["falha"] = f"XML da compensação inválido ({'; '.join(erros)}); {aviso_estorno}"
        out("\n   ATENÇÃO: " + res["falha"])
        return res
    out(f"\n[B] Compensando {len(plano['atuais'])} parcela(s) antiga(s) + {doc_novo}/{item_contra} (ID {id_clear})...")
    try:
        enviar_wsrm(s, env_clear, ep_clear, ACT_CLEAR, "Compensação", out)
    except Exception as e:
        res["falha"] = f"compensação recusada ({e}); {aviso_estorno}"
        out("   ATENÇÃO: " + res["falha"])
        return res

    # Confirmação: parcelas antigas + contrapartida (+ adiantamento) compensadas
    campo_doc, _ = _campos_compensacao(plano["atuais"][0])
    if not campo_doc:
        res["falha"] = (f"a CDS não informa compensação: confira no app 'Administrar itens fornecedor' "
                        f"(fornecedor {fatura['InvoicingParty']}) ou no Message Dashboard, ID {id_clear}.")
        out("\n" + res["falha"])
        return res
    for i in range(1, POLL_TENTATIVAS + 1):
        time.sleep(POLL_INTERVALO_S)
        antigas = [l for l in leitor.linhas(documento, ano, empresa) if l.get("Supplier")
                   and not str(l.get("SpecialGLCode") or "").strip()]
        novas = leitor.linhas(doc_novo, ano_novo, empresa)
        contra = [l for l in novas if str(l.get("AccountingDocumentItem")).zfill(3) == item_contra]
        pend = [l for l in antigas + contra if not str(l.get(campo_doc) or "").strip()]
        if antigas and contra and not pend:
            res.update(ok=True, doc_comp=contra[0].get(campo_doc))
            out(f"\nCONCLUÍDO: parcelas antigas e contrapartida compensadas pelo documento {res['doc_comp']}.")
            out(f"Em aberto ficam só as parcelas novas do documento {doc_novo}.")
            if plano["novas"] and plano["novas"][0]["antecipada"]:
                out("A 1ª parcela nova está bloqueada (A): compensar com o adiantamento indicado acima.")
            return res
        out(f"   tentativa {i}: {len(pend)} item(ns) ainda sem compensação")
    res["falha"] = (f"compensação não confirmada no tempo de espera (Message Dashboard, ID {id_clear}). "
                    f"Se não entrar, o documento {doc_novo} fica com parcelas em dobro: estorne-o.")
    out("\n" + res["falha"])
    return res


def main_alterar_data(a, plano, leitor, documento):
    base_id = uuid.uuid4().hex[:8].upper()
    l0, alvo0 = plano["datas"][0]
    exemplos = [montar_change(f"AJDATA_{base_id}_TPT", a.empresa, a.ano, documento, l0["AccountingDocumentItem"],
                              "PaymentTermsChange", "PaymentTerms", ""),
                montar_change(f"AJDATA_{base_id}_TD", a.empresa, a.ano, documento, l0["AccountingDocumentItem"],
                              "DueCalculationBaseDateChange", "DueCalculationBaseDate", alvo0.isoformat())]
    # Valor vazio é o jeito de LIMPAR o campo (validado pelo colega em QAS); o schema pede minLength 1,
    # mas o SAP aceita. Só esse caso é ignorado.
    erros = [e for env in exemplos for e in validar(env, WSDL_CHANGE, "Change")
             if not ("'PaymentTerms'" in e and "minLength" in e)]
    if erros:
        print("XML INVÁLIDO contra o WSDL -- nada será enviado:", *erros, sep="\n   - ")
        sys.exit(1)
    print("XMLs válidos contra o WSDL.")
    if a.mostrar_xml:
        for env in exemplos:
            print(etree.tostring(env, pretty_print=True, encoding="unicode"))
    if not a.enviar:
        print("\nSIMULAÇÃO -- nada foi enviado. Use --enviar para executar.")
        return
    if leitor is None:
        sys.exit("Modo offline não envia.")
    if input("\nConfirma o envio ao SAP? [s/n]: ").strip().lower() != "s":
        sys.exit("Cancelado.")
    from config import SAP_USERNAME, SAP_PASSWORD
    from requests.auth import HTTPBasicAuth
    s = requests.Session()
    s.auth = HTTPBasicAuth(SAP_USERNAME, SAP_PASSWORD)
    ep = _endpoint(WSDL_CHANGE, leitor.sc.SAP_BASE_URL.rstrip("/") + "/sap/bc/srt/scs_ext/sap/journalentrybulkchangerequest_")
    ok = executar_alteracao_datas(s, leitor, plano, a.empresa, a.ano, documento, ep, base_id)
    print("\nCONCLUÍDO: vencimentos iguais ao XML." if ok else "\nNão concluído -- veja as mensagens acima.")


def conferir_parcelas_novas(plano, linhas_novas, item_contra, out=print):
    """Confere no SAP (CDS) tipo, valor, vencimento e bloqueio das parcelas criadas."""
    criadas = sorted([l for l in linhas_novas if str(l.get("AccountingDocumentItem")).zfill(3) != item_contra],
                     key=lambda l: str(l.get("AccountingDocumentItem")))
    problemas = []
    if len(criadas) != len(plano["novas"]):
        problemas.append(f"{len(criadas)} parcela(s) no SAP, esperado {len(plano['novas'])}")
    for l, p in zip(criadas, plano["novas"]):
        item = l.get("AccountingDocumentItem")
        if "AccountingDocumentType" in l and l.get("AccountingDocumentType") != TIPO_DOC_NOVAS:
            problemas.append(f"item {item}: tipo {l.get('AccountingDocumentType')} (esperado {TIPO_DOC_NOVAS})")
        if abs(-_dec(l["AmountInCompanyCodeCurrency"]) - p["valor"]) > TOLERANCIA_IGUAL:
            problemas.append(f"item {item}: valor {-_dec(l['AmountInCompanyCodeCurrency'])} (esperado {p['valor']})")
        if _data_odata(l.get("NetDueDate")) != p["data_base"]:
            problemas.append(f"item {item}: vencimento {_data_odata(l.get('NetDueDate'))} (esperado {p['data_base']})")
        if "PaymentBlockingReason" in l and str(l.get("PaymentBlockingReason") or "").strip() != p["bloqueio"]:
            problemas.append(f"item {item}: bloqueio '{l.get('PaymentBlockingReason') or ''}' (esperado '{p['bloqueio']}')")
    if problemas:
        out("   ATENÇÃO -- parcelas novas diferentes do planejado:\n     - " + "\n     - ".join(problemas))
    else:
        out("   Parcelas novas conferidas no SAP: tipo, valor, vencimento e bloqueio OK.")
    return problemas


# ----------------------------------------------------------------------
# Parcela antecipada x adiantamento: compensação (passo C)
# ----------------------------------------------------------------------
def _ref_item(texto):
    """'1505432702/2026/001' -> ('1505432702', '2026', '001')."""
    partes = str(texto).strip().split("/")
    if len(partes) != 3 or not all(partes):
        raise ValueError(f"use DOCUMENTO/ANO/ITEM (ex.: 1505432702/2026/001), recebi '{texto}'")
    return partes[0], partes[1], partes[2].zfill(3)


def escolher_adiantamento(valor: Decimal, candidatos: list, card_id=None):
    """Adiantamento (razão especial A, em aberto) com o mesmo valor; com vários, só o que tiver o card.
    Devolve (escolhido ou None, nº de candidatos com o mesmo valor)."""
    mesmos = [c for c in candidatos if str(c.get("SpecialGLCode") or "").strip() == "A"
              and not str(c.get("ClearingJournalEntry") or "").strip()
              and abs(abs(_dec(c["AmountInCompanyCodeCurrency"])) - valor) <= TOLERANCIA_IGUAL]
    com_card = [c for c in mesmos if card_id and card_id in (str(c.get("DocumentItemText") or "") + " "
                                                             + str(c.get("AssignmentReference") or ""))]
    if len(com_card) == 1:
        return com_card[0], len(mesmos)
    return (mesmos[0] if len(mesmos) == 1 else None), len(mesmos)


def _chave_item(l):
    return (str(l.get("AccountingDocument") or ""), str(l.get("FiscalYear") or ""),
            str(l.get("AccountingDocumentItem") or "").zfill(3))


def tem_card(item: dict, card_id) -> bool:
    """O ID do card aparece (como número inteiro) no texto do item, na atribuição ou na referência."""
    card = str(card_id or "").strip()
    if len(card) < 6:
        return False
    txt = " ".join(str(item.get(k) or "") for k in ("DocumentItemText", "AssignmentReference", "DocumentReferenceID",
                                                    "Reference1IDByBusinessPartner", "Reference2IDByBusinessPartner",
                                                    "Reference3IDByBusinessPartner"))
    return re.search(rf"(?<!\d){re.escape(card)}(?!\d)", txt) is not None


def _num_parcela_no_texto(texto):
    """'id 1463331410, 2º PARCELA' -> 2; 'ID 1450103021, 5 PARCELA' -> 5; senão None."""
    m = re.search(r"(?<!\d)(\d{1,2})\s*(?:º|ª|°|O|A)?\s*PARC", str(texto or "").upper())
    return int(m.group(1)) if m else None


def casar_adiantamentos_do_card(parcelas: list, candidatos: list, card_id, excluir=()) -> list:
    """Adiantamentos pagos ANTES do lançamento para qualquer parcela (NF 44383: 'id 1463331410, 2º PARCELA').
    Só considera adiantamento A em aberto com o ID do card no texto/atribuição/referência e o MESMO valor de uma
    parcela em aberto do lançamento. Com várias parcelas do mesmo valor, escolhe: nº da parcela citado no texto
    do adiantamento > vencimento igual à data do adiantamento > vencimento mais antigo.
    parcelas: itens do lançamento (todos, para numerar as parcelas). Devolve [(parcela, adiantamento, nº)]."""
    todas = sorted([l for l in parcelas if str(l.get("Supplier") or "").strip() and l.get("NetDueDate")
                    and not str(l.get("SpecialGLCode") or "").strip()
                    and _dec(l.get("AmountInCompanyCodeCurrency") or 0) < 0],
                   key=lambda l: str(l.get("AccountingDocumentItem") or ""))
    numero = {_chave_item(l): i + 1 for i, l in enumerate(todas)}
    excluir = set(excluir or ())
    livres = [l for l in todas if not str(l.get("ClearingJournalEntry") or "").strip() and _chave_item(l) not in excluir]
    adtos = [c for c in candidatos if str(c.get("SpecialGLCode") or "").strip() == "A"
             and not str(c.get("ClearingJournalEntry") or "").strip()
             and _dec(c.get("AmountInCompanyCodeCurrency") or 0) > 0
             and _chave_item(c) not in excluir and tem_card(c, card_id)]
    adtos.sort(key=lambda c: (_data_odata(c.get("PostingDate")) or _data_odata(c.get("DocumentDate")) or date.max,
                              _chave_item(c)))
    pares = []
    for a in adtos:
        valor = _dec(a["AmountInCompanyCodeCurrency"])
        mesmos = [l for l in livres if abs(-_dec(l["AmountInCompanyCodeCurrency"]) - valor) <= TOLERANCIA_IGUAL]
        if not mesmos:
            continue
        n = _num_parcela_no_texto(a.get("DocumentItemText"))
        dt = _data_odata(a.get("PostingDate")) or _data_odata(a.get("DocumentDate"))
        if n is not None:
            # o texto cita a parcela: só ela serve (se já estiver compensada ou com outro valor, não adivinho)
            escolha = next((l for l in mesmos if numero[_chave_item(l)] == n), None)
        else:
            escolha = (next((l for l in mesmos if dt and _data_odata(l.get("NetDueDate")) == dt), None)
                       or min(mesmos, key=lambda l: (_data_odata(l.get("NetDueDate")) or date.max, _chave_item(l))))
        if escolha is None:
            continue
        livres.remove(escolha)
        pares.append((escolha, a, numero[_chave_item(escolha)]))
    return pares


def montar_clearing_itens(empresa, fornecedor, itens, message_id, texto):
    """Compensação simples de itens existentes do fornecedor. itens = [(doc, ano, item), ...]."""
    env, body = _envelope_vazio()
    root = _sub(body, f"{{{SFIN_NS}}}JournalEntryBulkClearingRequest")
    mh = _sub(root, "MessageHeader")
    _sub(mh, "ID", message_id)
    _sub(mh, "CreationDateTime", _agora())
    req = _sub(root, "JournalEntryClearingRequest")
    mh2 = _sub(req, "MessageHeader")
    _sub(mh2, "ID", f"{message_id}_1")
    _sub(mh2, "CreationDateTime", _agora())
    je = _sub(req, "JournalEntry")
    hoje = date.today().isoformat()
    _sub(je, "CompanyCode", empresa)
    _sub(je, "AccountingDocumentType", TIPO_DOC_COMPENSACAO)
    _sub(je, "DocumentDate", hoje)
    _sub(je, "PostingDate", hoje)
    _sub(je, "CurrencyCode", "BRL")
    _sub(je, "DocumentHeaderText", texto[:25])
    for ref, (docu, ano, item) in enumerate(itens, start=1):
        ap = _sub(je, "APARItems")
        _sub(ap, "ReferenceDocumentItem", str(ref))
        _sub(ap, "CompanyCode", empresa)
        _sub(ap, "AccountType", "K")
        _sub(ap, "APARAccount", fornecedor)
        _sub(ap, "FiscalYear", ano)
        _sub(ap, "AccountingDocument", docu)
        _sub(ap, "AccountingDocumentItem", str(item).zfill(3))
    return env


def preparar_compensacao_adiantamento(parcela: dict, adto: dict, fornecedor: str, empresa: str, nf: str,
                                      message_id: str):
    """Confere a parcela (crédito, em aberto, sem razão especial) e o adiantamento (A, débito, em aberto,
    mesmo valor) e monta o XML. Devolve (env, problemas)."""
    problemas = []
    vp, va = _dec(parcela["AmountInCompanyCodeCurrency"]), _dec(adto["AmountInCompanyCodeCurrency"])
    if str(parcela.get("Supplier") or "").strip() != fornecedor or str(adto.get("Supplier") or "").strip() != fornecedor:
        problemas.append("parcela e adiantamento precisam ser do mesmo fornecedor da fatura")
    if str(parcela.get("ClearingJournalEntry") or "").strip():
        problemas.append(f"a parcela já está compensada (doc. {parcela.get('ClearingJournalEntry')})")
    if str(parcela.get("SpecialGLCode") or "").strip():
        problemas.append("a parcela é de razão especial; esperado item normal")
    if vp >= 0:
        problemas.append(f"a parcela não é um crédito do fornecedor (valor {vp})")
    if str(adto.get("SpecialGLCode") or "").strip() != "A":
        problemas.append("o adiantamento não é razão especial A")
    if str(adto.get("ClearingJournalEntry") or "").strip():
        problemas.append(f"o adiantamento já está compensado (doc. {adto.get('ClearingJournalEntry')})")
    if va <= 0:
        problemas.append(f"o adiantamento não é um débito (valor {va})")
    if abs(vp + va) > TOLERANCIA_IGUAL:
        problemas.append(f"valores diferentes: parcela {abs(vp)} x adiantamento {abs(va)}")
    itens = [(parcela["AccountingDocument"], str(parcela["FiscalYear"]), str(parcela["AccountingDocumentItem"])),
             (adto["AccountingDocument"], str(adto["FiscalYear"]), str(adto["AccountingDocumentItem"]))]
    env = montar_clearing_itens(empresa, fornecedor, itens, message_id, f"Comp adto NF {nf}")
    return env, problemas


def compensar_parcela_com_adiantamento(s, leitor, parcela: dict, adto: dict, fornecedor: str, empresa: str,
                                       nf: str, message_id=None, out=print):
    """Passo C: compensa a parcela antecipada contra o adiantamento (os dois somam zero).
    Devolve {"ok", "doc_comp", "falha"}. Se falhar, nada muda: a parcela continua bloqueada (A)."""
    res = {"ok": False, "doc_comp": None, "falha": None}
    message_id = message_id or f"AJADTO_{uuid.uuid4().hex[:10].upper()}"
    env, problemas = preparar_compensacao_adiantamento(parcela, adto, fornecedor, empresa, nf, message_id)
    problemas += validar(env, WSDL_CLEAR, "Compensação adiantamento")
    if problemas:
        res["falha"] = "; ".join(problemas)
        out("   compensação com o adiantamento não enviada: " + res["falha"])
        return res
    ep_clear = _endpoint(WSDL_CLEAR, leitor.sc.SAP_BASE_URL.rstrip("/") + "/sap/bc/srt/scs_ext/sap/journalentrybulkclearingreques")
    out(f"\n[C] Compensando a parcela {parcela['AccountingDocument']}/{parcela['AccountingDocumentItem']} com o "
        f"adiantamento {adto['AccountingDocument']}/{adto['AccountingDocumentItem']} (ID {message_id})...")
    try:
        enviar_wsrm(s, env, ep_clear, ACT_CLEAR, "Compensação adiantamento", out)
    except Exception as e:
        res["falha"] = f"compensação com o adiantamento recusada ({e})"
        out("   " + res["falha"])
        return res

    def _atual(l):
        for x in leitor.linhas(l["AccountingDocument"], str(l["FiscalYear"]), empresa):
            if str(x.get("AccountingDocumentItem")).zfill(3) == str(l["AccountingDocumentItem"]).zfill(3):
                return x
        return {}

    for i in range(1, POLL_TENTATIVAS + 1):
        time.sleep(POLL_INTERVALO_S)
        p, a = _atual(parcela), _atual(adto)
        dp, da = str(p.get("ClearingJournalEntry") or "").strip(), str(a.get("ClearingJournalEntry") or "").strip()
        if dp and da:
            res.update(ok=True, doc_comp=dp)
            out(f"   CONCLUÍDO: parcela e adiantamento compensados pelo documento {dp}.")
            return res
        out(f"   tentativa {i}: ainda sem compensação")
    res["falha"] = (f"compensação com o adiantamento não confirmada no tempo de espera (Message Dashboard, "
                    f"ID {message_id}). A parcela continua bloqueada (A): compensar manualmente.")
    out("   " + res["falha"])
    return res


def main_compensar_adiantamento(a):
    """CLI: --compensar-parcela DOC/ANO/ITEM [--adiantamento DOC/ANO/ITEM] [--enviar]."""
    leitor = LeitorSAP()
    fatura = leitor.fatura(a.fatura, a.ano)
    fornecedor = str(fatura.get("InvoicingParty") or "").strip()
    nf = str(fatura.get("SupplierInvoiceIDByInvcgParty") or a.fatura)
    doc, ano, item = _ref_item(a.compensar_parcela)
    parcela = next((l for l in leitor.linhas(doc, ano, a.empresa)
                    if str(l.get("AccountingDocumentItem")).zfill(3) == item), None)
    if not parcela:
        sys.exit(f"Não achei a linha {doc}/{ano}/{item} na CDS.")
    valor = abs(_dec(parcela["AmountInCompanyCodeCurrency"]))
    if a.adiantamento:
        d2, a2, i2 = _ref_item(a.adiantamento)
        adto = next((l for l in leitor.linhas(d2, a2, a.empresa)
                     if str(l.get("AccountingDocumentItem")).zfill(3) == i2), None)
        n = 1 if adto else 0
    else:
        adto, n = escolher_adiantamento(valor, leitor.adiantamentos_abertos(fornecedor, a.empresa),
                                        _card_id_das_parcelas([parcela]))
    print("=" * 78)
    print(f"Fornecedor {fornecedor} | NF {nf}")
    print(f"Parcela:       {doc}/{ano} item {item}  R$ {valor}  venc. {_data_odata(parcela.get('NetDueDate'))}  "
          f"bloqueio '{parcela.get('PaymentBlockingReason') or ''}'  compensada: '{parcela.get('ClearingJournalEntry') or ''}'")
    if not adto:
        sys.exit(f"Adiantamento não identificado com segurança ({n} candidato(s) com R$ {valor}). "
                 f"Informe com --adiantamento DOC/ANO/ITEM.")
    print(f"Adiantamento:  {adto['AccountingDocument']}/{adto['FiscalYear']} item {adto['AccountingDocumentItem']}  "
          f"R$ {abs(_dec(adto['AmountInCompanyCodeCurrency']))}  razão esp. '{adto.get('SpecialGLCode') or ''}'  "
          f"atrib. '{adto.get('AssignmentReference') or ''}'")
    env, problemas = preparar_compensacao_adiantamento(parcela, adto, fornecedor, a.empresa, nf, "AJADTO_TESTE")
    problemas += validar(env, WSDL_CLEAR, "Compensação adiantamento")
    print("=" * 78)
    if problemas:
        sys.exit("Nada será enviado:\n   - " + "\n   - ".join(problemas))
    print("XML válido contra o WSDL.")
    if a.mostrar_xml:
        print(etree.tostring(env, pretty_print=True, encoding="unicode"))
    if not a.enviar:
        print("\nSIMULAÇÃO -- nada foi enviado. Use --enviar para executar.")
        return
    if input("\nConfirma a compensação no SAP? [s/n]: ").strip().lower() != "s":
        sys.exit("Cancelado.")
    from config import SAP_USERNAME, SAP_PASSWORD
    from requests.auth import HTTPBasicAuth
    s = requests.Session()
    s.auth = HTTPBasicAuth(SAP_USERNAME, SAP_PASSWORD)
    res = compensar_parcela_com_adiantamento(s, leitor, parcela, adto, fornecedor, a.empresa, nf)
    if not res["ok"]:
        sys.exit(2)


def main_compensar_itens(a):
    """CLI: --compensar-itens DOC/ANO/ITEM ... -- compensa itens em aberto do mesmo fornecedor que somam zero
    (ex.: parcelas antigas + contrapartida de um reparcelamento cuja compensação o SAP não processou)."""
    leitor = LeitorSAP()
    linhas = []
    for ref in a.compensar_itens:
        partes = str(ref).strip().split("/")
        if len(partes) == 2 and all(partes):
            # DOC/ANO: todos os itens normais do fornecedor ainda em aberto do documento
            # (ex.: lançamento estornado + documento de estorno que ficaram em aberto -- NF 44383)
            abertos = [x for x in leitor.linhas(partes[0], partes[1], a.empresa)
                       if str(x.get("Supplier") or "").strip() and not str(x.get("SpecialGLCode") or "").strip()
                       and not str(x.get("ClearingJournalEntry") or "").strip()]
            if not abertos:
                sys.exit(f"Nenhum item do fornecedor em aberto no documento {partes[0]}/{partes[1]}.")
            linhas += sorted(abertos, key=lambda x: str(x.get("AccountingDocumentItem")))
            continue
        doc, ano, item = _ref_item(ref)
        l = next((x for x in leitor.linhas(doc, ano, a.empresa)
                  if str(x.get("AccountingDocumentItem")).zfill(3) == item), None)
        if not l:
            sys.exit(f"Não achei a linha {doc}/{ano}/{item} na CDS.")
        linhas.append(l)
    print("=" * 78)
    problemas = []
    fornecedores = {str(l.get("Supplier") or "").strip() for l in linhas}
    for l in linhas:
        print(f"  {l['AccountingDocument']}/{l['FiscalYear']} item {l['AccountingDocumentItem']}  "
              f"R$ {_dec(l['AmountInCompanyCodeCurrency']):>12}  venc. {_data_odata(l.get('NetDueDate'))}  "
              f"compensado: '{l.get('ClearingJournalEntry') or ''}'  {l.get('DocumentItemText') or ''}")
        if str(l.get("ClearingJournalEntry") or "").strip():
            problemas.append(f"{l['AccountingDocument']}/{l['AccountingDocumentItem']} já está compensado")
    soma = sum((_dec(l["AmountInCompanyCodeCurrency"]) for l in linhas), Decimal("0"))
    print(f"  Soma: R$ {soma}")
    if len(fornecedores) != 1 or "" in fornecedores:
        problemas.append(f"os itens precisam ser do mesmo fornecedor (achei {sorted(fornecedores)})")
    if abs(soma) > TOLERANCIA_IGUAL:
        problemas.append(f"os itens não somam zero (soma {soma})")
    fornecedor = next(iter(fornecedores))
    message_id = f"AJCOMP_{uuid.uuid4().hex[:10].upper()}"
    itens = [(l["AccountingDocument"], str(l["FiscalYear"]), str(l["AccountingDocumentItem"])) for l in linhas]
    env = montar_clearing_itens(a.empresa, fornecedor, itens, message_id, f"Comp ajuste parcelas")
    problemas += validar(env, WSDL_CLEAR, "Compensação")
    print("=" * 78)
    if problemas:
        sys.exit("Nada será enviado:\n   - " + "\n   - ".join(problemas))
    print("XML válido contra o WSDL.")
    if a.mostrar_xml:
        print(etree.tostring(env, pretty_print=True, encoding="unicode"))
    if not a.enviar:
        print("\nSIMULAÇÃO -- nada foi enviado. Use --enviar para executar.")
        return
    if input("\nConfirma a compensação no SAP? [s/n]: ").strip().lower() != "s":
        sys.exit("Cancelado.")
    from config import SAP_USERNAME, SAP_PASSWORD
    from requests.auth import HTTPBasicAuth
    s = requests.Session()
    s.auth = HTTPBasicAuth(SAP_USERNAME, SAP_PASSWORD)
    ep_clear = _endpoint(WSDL_CLEAR, leitor.sc.SAP_BASE_URL.rstrip("/") + "/sap/bc/srt/scs_ext/sap/journalentrybulkclearingreques")
    print(f"\nCompensando {len(itens)} item(ns) (ID {message_id})...")
    enviar_wsrm(s, env, ep_clear, ACT_CLEAR, "Compensação", print)
    for i in range(1, POLL_TENTATIVAS + 1):
        time.sleep(POLL_INTERVALO_S)
        pend = []
        for l in linhas:
            atual = next((x for x in leitor.linhas(l["AccountingDocument"], str(l["FiscalYear"]), a.empresa)
                          if str(x.get("AccountingDocumentItem")) == str(l["AccountingDocumentItem"])), {})
            if not str(atual.get("ClearingJournalEntry") or "").strip():
                pend.append(l)
            else:
                doc_comp = atual.get("ClearingJournalEntry")
        if not pend:
            print(f"CONCLUÍDO: itens compensados pelo documento {doc_comp}.")
            return
        print(f"   tentativa {i}: {len(pend)} item(ns) ainda sem compensação")
    sys.exit(f"Compensação não confirmada no tempo de espera. Veja o Message Dashboard, ID {message_id}.")



def montar_estorno(documento, ano, empresa, message_id, motivo=MOTIVO_ESTORNO, referencia=None, data=None, tipo=None):
    """Post de estorno: só cabeçalho, apontando para o lançamento original (BKPFF: chave = DOC+EMPRESA+ANO).
    O SAP gera o documento inverso e compensa os itens do original com ele."""
    data = (data or date.today()).isoformat()
    env, body = _envelope_vazio()
    root = _sub(body, f"{{{SFIN_NS}}}JournalEntryBulkCreateRequest")
    mh = _sub(root, "MessageHeader")
    _sub(mh, "ID", message_id)
    _sub(mh, "CreationDateTime", _agora())
    req = _sub(root, "JournalEntryCreateRequest")
    mh2 = _sub(req, "MessageHeader")
    _sub(mh2, "ID", f"{message_id}_1")
    _sub(mh2, "CreationDateTime", _agora())
    je = _sub(req, "JournalEntry")
    _sub(je, "OriginalReferenceDocumentType", "BKPFF")
    _sub(je, "BusinessTransactionType", "RFBU")
    _sub(je, "AccountingDocumentType", tipo or TIPO_DOC_NOVAS)
    _sub(je, "DocumentHeaderText", f"Estorno {documento}"[:25])
    _sub(je, "ReversalReason", motivo)
    _sub(je, "ReversalReferenceDocument", referencia or f"{documento}{empresa}{ano}")
    _sub(je, "CreatedByUser", CRIADO_POR[:12])
    _sub(je, "CompanyCode", empresa)
    _sub(je, "DocumentDate", data)
    _sub(je, "PostingDate", data)
    return env


def main_estornar(a):
    """CLI: --estornar DOC/ANO -- estorna um KZ de ajuste de parcelas lançado pela automação (nenhum item
    compensado). Ex.: o 1542040222 da NF 359599, cuja compensação o SAP recusou por falta de IRF."""
    partes = str(a.estornar).strip().split("/")
    if len(partes) != 2 or not all(partes):
        sys.exit(f"use --estornar DOCUMENTO/ANO (ex.: 1542040222/2026), recebi '{a.estornar}'")
    documento, ano = partes
    leitor = LeitorSAP()
    fatura = leitor.fatura(a.fatura, a.ano)
    fornecedor = str(fatura.get("InvoicingParty") or "").strip()
    linhas = leitor.linhas(documento, ano, a.empresa)
    print("=" * 78)
    print(f"Estorno do lançamento {documento}/{ano} ({a.empresa}) -- fatura {a.fatura}, fornecedor {fornecedor}")
    problemas = []
    if not linhas:
        problemas.append(f"não achei {documento}/{ano} na CDS de itens do fornecedor")
    for l in linhas:
        print(f"  item {l['AccountingDocumentItem']}  {l.get('AccountingDocumentType') or '':2}  "
              f"fornecedor {l.get('Supplier') or '':10}  R$ {_dec(l['AmountInCompanyCodeCurrency']):>12}  "
              f"venc. {_data_odata(l.get('NetDueDate'))}  compensado: '{l.get('ClearingJournalEntry') or ''}'  "
              f"{l.get('DocumentItemText') or ''}")
        if str(l.get("ClearingJournalEntry") or "").strip():
            problemas.append(f"item {l['AccountingDocumentItem']} já está compensado "
                             f"({l.get('ClearingJournalEntry')}) -- estorne no app")
        if l.get("Supplier") and str(l["Supplier"]).strip() != fornecedor:
            problemas.append(f"item {l['AccountingDocumentItem']} é do fornecedor {l['Supplier']}, não {fornecedor}")
    tipos = {str(l.get("AccountingDocumentType") or "").strip() for l in linhas}
    if linhas and (len(tipos) != 1 or not tipos <= set(TIPOS_DOC_AJUSTE)):
        problemas.append(f"só estorno documento de ajuste {'/'.join(TIPOS_DOC_AJUSTE)} (achei {sorted(tipos)})")
    if linhas and not any("ajuste NF" in str(l.get("DocumentItemText") or "") for l in linhas):
        problemas.append("não parece um KZ de ajuste da automação (nenhum item com 'ajuste NF' no texto)")
    message_id = f"AJEST_{uuid.uuid4().hex[:10].upper()}"
    tipo_doc = next(iter(tipos)) if len(tipos) == 1 else TIPO_DOC_NOVAS
    env = montar_estorno(documento, ano, a.empresa, message_id, a.motivo, a.referencia, tipo=tipo_doc)
    problemas += validar(env, WSDL_POST, "Estorno")
    print(f"Motivo {a.motivo}, data de lançamento {date.today():%d/%m/%Y}, referência "
          f"{a.referencia or documento + a.empresa + ano}")
    print("=" * 78)
    if problemas:
        sys.exit("Nada será enviado:\n   - " + "\n   - ".join(problemas))
    print("XML válido contra o WSDL.")
    if a.mostrar_xml:
        print(etree.tostring(env, pretty_print=True, encoding="unicode"))
    if not a.enviar:
        print("\nSIMULAÇÃO -- nada foi enviado. Use --enviar para executar.")
        return
    if input(f"\nConfirma o ESTORNO de {documento}/{ano} no SAP? [s/n]: ").strip().lower() != "s":
        sys.exit("Cancelado.")
    from config import SAP_USERNAME, SAP_PASSWORD
    from requests.auth import HTTPBasicAuth
    s = requests.Session()
    s.auth = HTTPBasicAuth(SAP_USERNAME, SAP_PASSWORD)
    ep_post = _endpoint(WSDL_POST, leitor.sc.SAP_BASE_URL.rstrip("/") + "/sap/bc/srt/scs_ext/sap/journalentrycreaterequestconfi")
    print(f"\nEstornando (ID {message_id})...")
    doc_est, ano_est = enviar_post(s, env, ep_post, print)
    print(f"Documento de estorno: {doc_est}/{ano_est}")
    for i in range(1, POLL_TENTATIVAS + 1):
        abertos = [l for l in leitor.linhas(documento, ano, a.empresa)
                   if not str(l.get("ClearingJournalEntry") or "").strip()]
        if not abertos:
            print(f"CONCLUÍDO: {documento}/{ano} estornado; itens compensados pelo estorno {doc_est}.")
            return
        print(f"   tentativa {i}: {len(abertos)} item(ns) do {documento} ainda em aberto na CDS")
        time.sleep(POLL_INTERVALO_S)
    print(f"Estorno {doc_est} lançado, mas a CDS ainda mostra itens do {documento} em aberto. Confira no app.")



def plano_conversao(leitor, documento, ano, empresa, aceitar_manual=False):
    """Plano para trocar as parcelas em aberto de um documento de ajuste KZ por parcelas RE iguais.
    Devolve (plano, fatura, dados_nf, ano_kz) ou levanta ValueError com o motivo de não converter."""
    linhas = [l for l in leitor.linhas(documento, ano, empresa) if str(l.get("Supplier") or "").strip()]
    if not linhas:
        raise ValueError("não achei o documento na CDS de itens do fornecedor")
    tipos = {str(l.get("AccountingDocumentType") or "").strip() for l in linhas}
    if tipos != {"KZ"}:
        raise ValueError(f"não é KZ (tipo {sorted(tipos)})")
    if not aceitar_manual and not any("NF " in str(l.get("DocumentItemText") or "") and ("Parcela" in str(l.get("DocumentItemText") or "")
               or "ajuste" in str(l.get("DocumentItemText") or "")) for l in linhas):
        raise ValueError("não parece um documento de ajuste da automação (texto sem 'Parcela ... NF')")
    fornecedor = str(linhas[0]["Supplier"]).strip()
    contra = [l for l in linhas if _dec(l["AmountInCompanyCodeCurrency"]) > 0]
    if len(contra) != 1 or not str(contra[0].get("ClearingJournalEntry") or "").strip():
        raise ValueError("a contrapartida do KZ não está compensada (ajuste incompleto) -- ver manualmente")
    abertas = [l for l in linhas if _dec(l["AmountInCompanyCodeCurrency"]) < 0
               and not str(l.get("ClearingJournalEntry") or "").strip()
               and not str(l.get("SpecialGLCode") or "").strip()]
    if not abertas:
        raise ValueError("nenhuma parcela em aberto (já pagas/compensadas) -- nada a converter")
    # fatura original: as parcelas compensadas junto com a contrapartida são as da fatura (RE)
    comp = str(contra[0]["ClearingJournalEntry"]).strip()
    orig = [l for l in leitor.linhas_compensadas_por(comp, fornecedor, empresa)
            if str(l.get("AccountingDocument")) != documento and str(l.get("AccountingDocumentType") or "") == "RE"]
    fatura, ano_fat = (None, None)
    if orig:
        fatura, ano_fat = leitor.fatura_do_documento(orig[0]["AccountingDocument"], str(orig[0]["FiscalYear"]), empresa)
    if not fatura:
        raise ValueError(f"não achei a fatura original (compensação {comp})")
    fat = leitor.fatura(fatura, ano_fat)
    if str(fat.get("InvoicingParty") or "").strip() != fornecedor:
        raise ValueError(f"fatura {fatura} é do fornecedor {fat.get('InvoicingParty')}, não {fornecedor}")
    nf = str(fat.get("SupplierInvoiceIDByInvcgParty") or "").split("-")[0].lstrip("0")
    card = next((m.group(1) for l in abertas for m in [re.search(r"ID (\d+)", str(l.get("DocumentItemText") or ""))] if m), None)
    abertas.sort(key=lambda l: (_data_odata(l.get("NetDueDate")) or date.max, str(l["AccountingDocumentItem"])))
    novas = [{"valor": -_dec(l["AmountInCompanyCodeCurrency"]), "data_base": _data_odata(l.get("NetDueDate")),
              "bloqueio": str(l.get("PaymentBlockingReason") or "").strip(),
              "texto": str(l.get("DocumentItemText") or "")[:50], "antecipada": False} for l in abertas]
    if any(p["data_base"] is None for p in novas):
        raise ValueError("parcela sem vencimento na CDS")
    total = sum((p["valor"] for p in novas), Decimal("0"))
    plano = {"acao": "ajustar", "motivo": "", "atuais": abertas, "novas": novas, "total": total,
             "total_contra": total, "card_id": card, "conversao_de": documento,
             "fatura_original": f"{fatura}/{ano_fat}", "doc_original": orig[0]["AccountingDocument"]}
    return plano, fat, {"numero_nf": nf}, ano


def main_converter(a):
    """CLI: --converter DOC/ANO ... -- parcelas em aberto de ajustes KZ passam a ser RE (Contas a Pagar, 08/10).
    Para cada KZ: Post RE com as mesmas parcelas (valor, vencimento, bloqueio, texto) + contrapartida e
    compensação das parcelas abertas do KZ com ela. O KZ fica no histórico, compensado. Se a compensação
    não entrar, o RE novo é estornado sozinho (nada fica em dobro)."""
    leitor = LeitorSAP()
    planos, pulados = [], []
    print("=" * 78)
    for ref in a.converter:
        partes = str(ref).strip().split("/")
        if len(partes) != 2 or not all(partes):
            sys.exit(f"use DOCUMENTO/ANO (ex.: 1562303084/2026), recebi '{ref}'")
        doc, ano = partes
        try:
            plano, fat, dados_nf, ano_kz = plano_conversao(leitor, doc, ano, a.empresa, a.aceitar_manual)
        except Exception as e:
            pulados.append((doc, str(e)))
            print(f"{doc}/{ano}: NÃO converto -- {e}")
            continue
        env = montar_post(plano, fat, dados_nf, a.empresa, "SIMULACAO")
        erros = validar(env, WSDL_POST, "Post")
        if erros:
            pulados.append((doc, "; ".join(erros)))
            print(f"{doc}/{ano}: XML inválido -- {'; '.join(erros)}")
            continue
        planos.append((doc, ano, plano, fat, dados_nf))
        print(f"{doc}/{ano} (KZ) -> {TIPO_DOC_NOVAS} | NF {dados_nf['numero_nf']} | fatura {plano['fatura_original']} "
              f"| fornecedor {fat['InvoicingParty']} | {len(plano['novas'])} parcela(s) em aberto, R$ {plano['total']}")
        for l, p in zip(plano["atuais"], plano["novas"]):
            print(f"     item {l['AccountingDocumentItem']}  venc. {p['data_base']}  R$ {p['valor']:>12}  "
                  f"bloqueio '{p['bloqueio']}'  {p['texto']}")
    print("=" * 78)
    print(f"A converter: {len(planos)} | não convertidos: {len(pulados)}")
    if not a.enviar:
        print("\nSIMULAÇÃO -- nada foi enviado. Use --enviar para executar.")
        return
    if not planos:
        return
    if input(f"\nDigite CONVERTER para lançar {len(planos)} documento(s) {TIPO_DOC_NOVAS} e compensar os KZ: ").strip() != "CONVERTER":
        sys.exit("Cancelado.")
    from config import SAP_USERNAME, SAP_PASSWORD
    from requests.auth import HTTPBasicAuth
    s = requests.Session()
    s.auth = HTTPBasicAuth(SAP_USERNAME, SAP_PASSWORD)
    resumo = []
    for doc, ano, plano, fat, dados_nf in planos:
        print(f"\n##### {doc}/{ano} (NF {dados_nf['numero_nf']})")
        try:
            res = executar_reparcelamento(s, leitor, plano, fat, dados_nf, a.empresa, ano, doc)
        except Exception as e:
            res = {"ok": False, "falha": f"erro: {e}"}
        resumo.append((doc, dados_nf["numero_nf"], res))
    print("\n" + "=" * 78)
    for doc, nf, res in resumo:
        if res.get("ok"):
            print(f"OK    {doc} (NF {nf}): parcelas em aberto agora no {TIPO_DOC_NOVAS} {res['doc_novo']} "
                  f"(compensação {res['doc_comp']})")
        else:
            print(f"FALHA {doc} (NF {nf}): {res.get('falha')}")
    for doc, motivo in pulados:
        print(f"PULADO {doc}: {motivo}")


if __name__ == "__main__":
    main()
