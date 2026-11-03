"""
sap_soap_client.py — Cliente SOAP (zeep + WS-ReliableMessaging manual) para os
serviços assíncronos de Journal Entry do SAP S/4HANA Cloud (acordo de
comunicação SAP_COM_0002), usados para corrigir automaticamente parcelas de
Contas a Pagar (data e valor) quando a Fase 2 (`_auditar_fase2_financeiro` em
`sap_client.py`) detecta divergência entre o que o SAP gravou e o que o XML
da NF realmente cobra.

Autenticação: reaproveita o mesmo usuário técnico e senha (`SAP_USERNAME`/
`SAP_PASSWORD`) já usados pelo cliente OData em `sap_client.py` — é o mesmo
`CB_NF_AUTO` configurado em "Comunicação de entrada" no acordo SAP_COM_0002,
autenticação básica (usuário/senha), sem credencial nova pra gerenciar.

DESCOBERTA CRÍTICA (25/09/2026): esse serviço exige WS-ReliableMessaging (WS-RM)
com SEQUÊNCIA EXPLÍCITA. Sem isso, o SAP aceita a mensagem via HTTP (200 OK)
mas recusa o processamento com um fault genérico ("Web service processing
error"), sem detalhar o motivo real na resposta. Isso foi descoberto só depois
de uma tentativa real falhar em produção (documento 5190602061/2026) e de um
teste confirmatório bem-sucedido contra o ambiente QAS da empresa (documento
9422245241/2026, tenant myXXXXXX) usando o handshake completo:

    1. CreateSequence          -> cliente envia, sem referenciar nada ainda
    2. CreateSequenceResponse  -> SAP devolve o Identifier real da sequência
    3. mensagem de negócio     -> referencia esse Identifier (wsrm:Sequence)
    4. TerminateSequence       -> encerra a sequência (boa prática)

NENHUMA lib Python popular (zeep, suds, etc.) implementa WS-RM nativamente.
Por isso este módulo usa uma abordagem híbrida:
  - `zeep` continua sendo usado só para MONTAR e VALIDAR o Body da mensagem
    de negócio contra o schema real do WSDL (via `client.create_message`,
    que roda o mesmo código de serialização/validação usado numa chamada
    normal, mas sem enviar nada pela rede). Foi o `zeep` que nos pegou os 2
    bugs reais de `MessageHeader` faltando antes de qualquer chamada de rede
    acontecer -- não queremos perder essa rede de segurança.
  - O envelope SOAP final (com os headers de WS-Addressing + WS-RM Sequence)
    é montado manualmente com `lxml`, injetando esses headers no envelope que
    o zeep já validou, e enviado via `requests` (reaproveitando a mesma sessão
    autenticada do `zeep.transports.Transport`).

Confirmação de sucesso: NÃO usamos o callback assíncrono oficial do SAP.
A "Comunicação de saída" do acordo foi deixada sem endpoint real configurado,
de propósito — em vez de expor um servidor SOAP nosso pra receber a
confirmação, o plano é disparar o request e, alguns segundos depois,
consultar de novo a `YY1_SUPPLIER_LINE_ITEMS_CDS` (mesmo padrão de
poll+retry que a Fase 2 já usa) pra confirmar que a mudança realmente colou.
Essa parte do polling ainda não está implementada aqui.

Estado deste módulo (25/09/2026, ainda não integrado a nada):
  - Carrega os 3 WSDLs locais (Change, Clearing, Creation) com zeep,
    autenticado, com cache de cliente por WSDL.
  - `montar_payload_change_data` + `enviar_change_data` (COM handshake WS-RM)
    prontos pra corrigir só a data-base de vencimento de uma parcela existente
    via JournalEntryBulkChangeRequest_In. Ainda NÃO testado contra produção
    com o WS-RM (só validado em QAS até agora, com um campo de teste
    diferente -- YY1_Status_Faturamento -- não com DueCalculationBaseDate).
  - Os payload builders de Clearing (estorno) e Creation (nova parcela), pro
    caso de divergência de VALOR, ainda não foram escritos, e ainda não
    sabemos se eles também exigem MessageHeader por item / WS-RM do mesmo
    jeito -- precisa introspectar cada um antes de assumir que é igual.
  - `main.py`/`sap_client.py` não chamam nada deste módulo ainda.

Estrutura do payload de Change (confirmada via introspecção real do zeep
contra o WSDL, 25/09/2026):
  JournalEntryBulkChangeRequest_In
    MessageHeader: {ID, CreationDateTime}  # CreationDateTime é obrigatório (min_occurs=1)
    JournalEntryDebtorCreditorItem: [{
        MessageHeader: {ID, CreationDateTime}  # cada ITEM também exige seu próprio MessageHeader
        ItemKey: {AccountingDocument, CompanyCode, FiscalYear, AccountingDocumentItemID}
        DueCalculationBaseDateChange: {DueCalculationBaseDate, FieldValueChangeIsRequested}
    }]
  (repare: existe também `AmountInPaymentCurrencyChange` nesse mesmo WSDL, mas é
  o campo de valor em "moeda de pagamento" — não é o `AmountInCompanyCodeCurrency`
  que a Fase 2 realmente audita. Por isso valor continua indo por Clearing+Creation,
  não por essa API de Change.)
"""
import os
import uuid
import logging
from datetime import date, datetime, timezone

from lxml import etree
from zeep import Client
from zeep.transports import Transport
from requests import Session
from requests.auth import HTTPBasicAuth

from config import SAP_USERNAME, SAP_PASSWORD

log = logging.getLogger(__name__)

_DIR_WSDL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apis_sap")

WSDL_CHANGE = os.path.join(_DIR_WSDL, "JOURNALENTRYBULKCHANGEREQUEST_.wsdl")
WSDL_CLEARING = os.path.join(_DIR_WSDL, "JOURNALENTRYBULKCLEARINGREQUES.wsdl")
WSDL_CREATION = os.path.join(_DIR_WSDL, "JOURNALENTRYBULKCREATIONREQUES.wsdl")

# Namespaces do WS-Addressing / WS-ReliableMessaging (fixos pelo padrão OASIS,
# não são específicos do nosso tenant).
_WSA_NS = "http://www.w3.org/2005/08/addressing"
_WSRM_NS = "http://docs.oasis-open.org/ws-rx/wsrm/200702"
_SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
_WSRM_NSMAP = {"wsa": _WSA_NS, "wsrm": _WSRM_NS, "soap-env": _SOAP_NS}

_clients: dict = {}


def _get_client(wsdl_path: str) -> Client:
    """Cria (e cacheia) um zeep.Client autenticado para o WSDL informado.
    Os WSDLs são lidos do disco local (pasta apis_sap/), não da URL do SAP
    embutida neles — só o endereço de destino das chamadas (soap:address)
    é que vem do SAP; o schema em si já temos aqui."""
    if wsdl_path not in _clients:
        session = Session()
        session.auth = HTTPBasicAuth(SAP_USERNAME, SAP_PASSWORD)
        transport = Transport(session=session, timeout=30, operation_timeout=60)
        _clients[wsdl_path] = Client(wsdl=wsdl_path, transport=transport)
    return _clients[wsdl_path]


def _endpoint_e_soapaction(client: Client, operation_name: str) -> tuple[str, str]:
    """Lê do próprio WSDL (não hardcoded) o endereço real de destino e o
    SOAPAction esperado pra uma operação -- evita depender de valores
    copiados de outro ambiente (ex.: o QAS usa tenant diferente do de
    produção, mas a estrutura do WSDL/SOAPAction é a mesma)."""
    binding = list(client.wsdl.bindings.values())[0]
    op = binding._operations[operation_name]
    soap_action = op.soapaction
    service = list(client.wsdl.services.values())[0]
    port = list(service.ports.values())[0]
    endpoint_url = port.binding_options["address"]
    return endpoint_url, soap_action


def testar_carregamento_wsdls() -> None:
    """Autoteste de fundação: só confirma que os 3 WSDLs carregam certo com
    zeep e que a operação esperada aparece em cada um. Não faz nenhuma
    chamada de rede real ao SAP (não precisa: carregar o WSDL local é
    processamento de XML puro)."""
    esperado = {
        WSDL_CHANGE: "JournalEntryBulkChangeRequest_In",
        WSDL_CLEARING: "JournalEntryBulkClearingRequest_In",
        WSDL_CREATION: "JournalEntryBulkCreationRequest_In",
    }
    for wsdl_path, op_esperada in esperado.items():
        client = _get_client(wsdl_path)
        operacoes = list(client.service._operations.keys())
        assert op_esperada in operacoes, (
            f"{os.path.basename(wsdl_path)}: esperava a operação '{op_esperada}', "
            f"achei {operacoes}"
        )
        print(f"OK  {os.path.basename(wsdl_path)}  -> {op_esperada}")


def _novo_message_header() -> dict:
    """MessageHeader mínimo exigido em todo request. O ID aqui é só pra log/
    correlação do lado do SAP -- não tem relação com o callback de confirmação
    (que decidimos não usar). CreationDateTime é o único campo do MessageHeader
    que o WSDL realmente exige (min_occurs=1) -- confirmado via introspecção
    do zeep contra o WSDL real em 25/09/2026. Os demais campos do
    BusinessDocumentMessageHeader (UUID, SenderBusinessSystemID,
    RecipientBusinessSystemID, BusinessScope etc.) são todos opcionais e
    foram deixados de fora de propósito."""
    return {
        "ID": str(uuid.uuid4()),
        "CreationDateTime": datetime.now(timezone.utc),
    }


def montar_payload_change_data(
    accounting_document: str,
    company_code: str,
    fiscal_year: str,
    accounting_document_item: str,
    nova_data_vencimento: date,
) -> dict:
    """Monta o payload de JournalEntryBulkChangeRequest_In pra corrigir SÓ a
    data-base de vencimento (DueCalculationBaseDate) de uma parcela já
    existente no SAP. Não mexe em valor nem em nenhum outro campo da parcela.

    Os 4 primeiros parâmetros identificam a parcela (mesmos campos que
    `_auditar_fase2_financeiro` em sap_client.py já busca: acc_doc, company_code,
    doc_year, e AccountingDocumentItem de cada linha em supplier_lines).

    Estrutura confirmada via dump recursivo do zeep contra o WSDL real em
    25/09/2026: além do MessageHeader do topo, cada item de
    JournalEntryDebtorCreditorItem também exige o SEU PRÓPRIO MessageHeader
    (min_occurs=1) -- é um campo obrigatório por item, não só no envelope
    geral. O resto da árvore desse WSDL (JournalEntryHeader, JournalEntryGLItem,
    SenderParty/RecipientParty, BusinessScope etc.) só vira obrigatório se a
    gente decidir incluir aquele bloco opcional -- não é o nosso caso aqui.
    """
    return {
        "MessageHeader": _novo_message_header(),
        "JournalEntryDebtorCreditorItem": [{
            "MessageHeader": _novo_message_header(),
            "ItemKey": {
                "AccountingDocument": accounting_document,
                "CompanyCode": company_code,
                "FiscalYear": fiscal_year,
                "AccountingDocumentItemID": accounting_document_item,
            },
            "DueCalculationBaseDateChange": {
                "DueCalculationBaseDate": nova_data_vencimento,
                "FieldValueChangeIsRequested": True,
            },
        }],
    }


def _post_soap(client: Client, envelope_bytes: bytes, soap_action: str, endpoint_url: str):
    """POST cru via a MESMA sessão autenticada (HTTPBasicAuth) que o zeep já
    usa internamente -- reaproveita `client.transport.session`, então não
    precisamos montar autenticação de novo aqui."""
    session = client.transport.session
    return session.post(
        endpoint_url,
        data=envelope_bytes,
        headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": soap_action},
        timeout=30,
    )


def _wsrm_header_envelope(endpoint_url: str, action: str, body_el) -> bytes:
    """Monta um envelope SOAP mínimo (Header WS-Addressing + Body informado)
    pros dois passos do handshake (CreateSequence / TerminateSequence), que
    não passam pelo zeep -- são chamadas de protocolo, não de negócio."""
    envelope = etree.Element(f"{{{_SOAP_NS}}}Envelope", nsmap={"soap-env": _SOAP_NS, "wsa": _WSA_NS, "wsrm": _WSRM_NS})
    header = etree.SubElement(envelope, f"{{{_SOAP_NS}}}Header")
    etree.SubElement(header, f"{{{_WSA_NS}}}To").text = endpoint_url
    etree.SubElement(header, f"{{{_WSA_NS}}}Action").text = action
    etree.SubElement(header, f"{{{_WSA_NS}}}MessageID").text = f"urn:uuid:{uuid.uuid4()}"
    reply_to = etree.SubElement(header, f"{{{_WSA_NS}}}ReplyTo")
    etree.SubElement(reply_to, f"{{{_WSA_NS}}}Address").text = f"{_WSA_NS}/anonymous"
    body = etree.SubElement(envelope, f"{{{_SOAP_NS}}}Body")
    body.append(body_el)
    return etree.tostring(envelope, xml_declaration=True, encoding="UTF-8")


def _wsrm_create_sequence(client: Client, endpoint_url: str) -> str:
    """Etapa 1/3 do handshake WS-RM: abre uma sequência nova e devolve o
    Identifier real que o SAP atribuiu (tem que ser usado tal e qual na
    mensagem de negócio -- não é um UUID que a gente inventa)."""
    action = f"{_WSRM_NS}/CreateSequence"
    create_seq_el = etree.Element(f"{{{_WSRM_NS}}}CreateSequence")
    acks_to = etree.SubElement(create_seq_el, f"{{{_WSRM_NS}}}AcksTo")
    etree.SubElement(acks_to, f"{{{_WSA_NS}}}Address").text = f"{_WSA_NS}/anonymous"

    envelope_bytes = _wsrm_header_envelope(endpoint_url, action, create_seq_el)
    resp = _post_soap(client, envelope_bytes, action, endpoint_url)
    resp.raise_for_status()

    root = etree.fromstring(resp.content)
    identifier_el = root.find(".//wsrm:CreateSequenceResponse/wsrm:Identifier", namespaces=_WSRM_NSMAP)
    if identifier_el is None:
        raise RuntimeError(f"CreateSequence não devolveu wsrm:Identifier. Resposta: {resp.text}")
    return identifier_el.text


def _wsrm_terminate_sequence(client: Client, endpoint_url: str, sequence_id: str) -> None:
    """Etapa 3/3 do handshake WS-RM: encerra a sequência. Boa prática, não é
    crítico -- se falhar, só loga e segue (a mensagem de negócio já foi
    processada na etapa 2, isso aqui é só limpeza do lado do SAP)."""
    action = f"{_WSRM_NS}/TerminateSequence"
    terminate_el = etree.Element(f"{{{_WSRM_NS}}}TerminateSequence")
    etree.SubElement(terminate_el, f"{{{_WSRM_NS}}}Identifier").text = sequence_id

    envelope_bytes = _wsrm_header_envelope(endpoint_url, action, terminate_el)
    try:
        resp = _post_soap(client, envelope_bytes, action, endpoint_url)
        resp.raise_for_status()
    except Exception:
        log.warning("TerminateSequence falhou (não crítico -- a mensagem de negócio já foi enviada)", exc_info=True)


def enviar_change_data(payload: dict):
    """Dispara JournalEntryBulkChangeRequest_In contra o SAP de verdade, COM
    o handshake completo de WS-ReliableMessaging que o serviço exige.
    CUIDADO: isso muda dado real de Contas a Pagar. Ainda não tem nenhuma
    chamada automática pra essa função em lugar nenhum do projeto -- só deve
    ser chamada manualmente, de propósito, num teste combinado antes.

    Como funciona:
      1. Usa `zeep` (client.create_message) só pra montar e VALIDAR o Body
         de negócio contra o schema do WSDL -- sem mandar nada pela rede
         ainda. Se o payload tiver campo obrigatório faltando, estoura aqui,
         igual antes.
      2. Abre uma sequência WS-RM nova (CreateSequence).
      3. Injeta os headers de WS-Addressing + WS-RM Sequence no envelope que
         o zeep validou, e manda a mensagem de negócio de verdade via
         `requests` (não mais via `client.service.Operation(...)`, que não
         sabe fazer esse handshake).
      4. Encerra a sequência (TerminateSequence) -- best-effort.

    Retorna o `requests.Response` cru da etapa 3 (a mensagem de negócio).
    Como a operação é assíncrona/one-way, uma resposta HTTP 200 aqui só
    confirma que o SAP aceitou a mensagem na sequência confiável -- NÃO
    confirma que o lançamento contábil já foi de fato alterado. Isso só se
    confirma consultando de novo a YY1_SUPPLIER_LINE_ITEMS_CDS (polling,
    ainda não implementado neste módulo)."""
    client = _get_client(WSDL_CHANGE)
    operation_name = "JournalEntryBulkChangeRequest_In"
    endpoint_url, soap_action = _endpoint_e_soapaction(client, operation_name)

    # 1) monta e valida o Body de negócio via zeep (sem enviar nada ainda)
    envelope = client.create_message(client.service, operation_name, **payload)

    # 2) abre uma sequência WS-RM nova
    sequence_id = _wsrm_create_sequence(client, endpoint_url)
    log.info("WS-RM: sequência aberta (%s) pra %s", sequence_id, operation_name)

    # 3) injeta os headers WS-Addressing + WS-RM Sequence no envelope já validado
    header = envelope.find(f"{{{_SOAP_NS}}}Header")
    if header is None:
        header = etree.Element(f"{{{_SOAP_NS}}}Header")
        envelope.insert(0, header)

    etree.SubElement(header, f"{{{_WSA_NS}}}To").text = endpoint_url
    etree.SubElement(header, f"{{{_WSA_NS}}}Action").text = soap_action
    etree.SubElement(header, f"{{{_WSA_NS}}}MessageID").text = f"urn:uuid:{uuid.uuid4()}"
    reply_to = etree.SubElement(header, f"{{{_WSA_NS}}}ReplyTo")
    etree.SubElement(reply_to, f"{{{_WSA_NS}}}Address").text = f"{_WSA_NS}/anonymous"
    sequence_el = etree.SubElement(header, f"{{{_WSRM_NS}}}Sequence")
    etree.SubElement(sequence_el, f"{{{_WSRM_NS}}}Identifier").text = sequence_id
    etree.SubElement(sequence_el, f"{{{_WSRM_NS}}}MessageNumber").text = "1"

    # 4) envia a mensagem de negócio de verdade
    envelope_bytes = etree.tostring(envelope, xml_declaration=True, encoding="UTF-8")
    resp = _post_soap(client, envelope_bytes, soap_action, endpoint_url)

    # 5) encerra a sequência (best-effort, não deixa exceção escapar)
    _wsrm_terminate_sequence(client, endpoint_url, sequence_id)

    resp.raise_for_status()
    return resp


if __name__ == "__main__":
    # Rodar direto (`python sap_soap_client.py`) só valida o carregamento dos
    # WSDLs -- não dispara nenhuma chamada real ao SAP.
    testar_carregamento_wsdls()
