"""
config.py — Configurações centrais da automação NF → SAP
"""
import os
from dotenv import load_dotenv

load_dotenv()

REQUIRED_VARS = ["SAP_PASSWORD", "PIPEFY_TOKEN", "WEBHOOK_SECRET", "BASIC_AUTH_PASSWORD", "BASIC_AUTH_USERNAME"]
for var in REQUIRED_VARS:
    if not os.getenv(var):
        raise EnvironmentError(f"ERRO CRÍTICO: Variável de ambiente '{var}' não encontrada no .env!")

# ── SAP ──────────────────────────────────────────────────────────────────────
SAP_BASE_URL    = os.getenv("SAP_BASE_URL", "https://myXXXXXX-api.s4hana.cloud.sap")
SAP_API_PATH    = "/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV"
SAP_AUTH_METHOD = os.getenv("SAP_AUTH_METHOD", "basic")
SAP_USERNAME    = os.getenv("SAP_USERNAME", "COMM_USER")
SAP_PASSWORD    = os.getenv("SAP_PASSWORD") 
SAP_OAUTH_TOKEN_URL     = os.getenv("SAP_OAUTH_TOKEN_URL")
SAP_OAUTH_CLIENT_ID     = os.getenv("SAP_OAUTH_CLIENT_ID")
SAP_OAUTH_CLIENT_SECRET = os.getenv("SAP_OAUTH_CLIENT_SECRET")

# ── Pipefy ────────────────────────────────────────────────────────────────────
PIPEFY_TOKEN   = os.getenv("PIPEFY_TOKEN")
PIPEFY_API_URL = "https://api.pipefy.com/graphql"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

PIPEFY_FIELDS = {
    "numero_pedido_sap":  "n_peido_sap"
}

TRIGGER_PHASE_ID = os.getenv("TRIGGER_PHASE_ID", "327192510")

# ── Matriz Enterprise de Filiais (Chaveada estritamente por CNPJ Destinatário) ──
MAPA_FILIAIS_CNPJ = {
    # Exemplo fictício. Os CNPJs e códigos reais ficam no .env/config local, fora do repositório.
    "11972834600515": {"company_code": "BR01", "uf": "GO", "business_place": "1001", "pop_type": "tipo_a", "doc_type": "RE", "nome": "Matriz"},
    "11209303657171": {"company_code": "BR01", "uf": "SP", "business_place": "1002", "pop_type": "tipo_a", "doc_type": "RE", "nome": "Filial SP"},
    "11374906635521": {"company_code": "BR01", "uf": "PR", "business_place": "1003", "pop_type": "tipo_a", "doc_type": "RE", "nome": "Filial PR"},
    "44382791465048": {"company_code": "BR02", "uf": "PR", "business_place": "2001", "pop_type": "tipo_b", "doc_type": "RE", "nome": "Empresa B"},
    "77223265752137": {"company_code": "BR03", "uf": "GO", "business_place": "3001", "pop_type": "tipo_b", "doc_type": "RE", "nome": "Empresa C"},
}

# ── Centro de custo x centro (Bloqueio de Centro de Custo, sap_client._erro_centro_custo) ──
# Prefixo do centro de custo por centro (exemplo fictício).
CC_PREFIXO_POR_CENTRO = {
    "1001": "01",   # Matriz
    "1002": "07",   # Filial SP
    "1003": "12",   # Filial PR
    "2001": "04",   # Empresa B
    "3001": "02",   # Empresa C
}
# Sobrescrever pelo .env, se precisar: CC_PREFIXO_POR_CENTRO=1001:01,1002:07,...
if os.getenv("CC_PREFIXO_POR_CENTRO", "").strip():
    CC_PREFIXO_POR_CENTRO = {c.strip(): p.strip() for c, p in (par.split(":", 1) for par in
                             os.getenv("CC_PREFIXO_POR_CENTRO").split(",") if ":" in par)}

# ── Basic Auth ──────────────────────────────────────────────────────────────────
BASIC_AUTH_USERNAME = os.getenv("BASIC_AUTH_USERNAME", "admin")
BASIC_AUTH_PASSWORD = os.getenv("BASIC_AUTH_PASSWORD")