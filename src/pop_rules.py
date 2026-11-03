"""
pop_rules.py — Auditoria Fiscal Rigorosa (POPs) em Shadow Mode
Audita o lançamento cruzando a Utilização do Pedido com a dedutibilidade REAL
(Documento Fiscal Brasileiro) extraída da API_LOGBR_NOTAFISCAL_SRV.
O resultado é devolvido como texto para comentário e preenchimento de campo no Pipefy.

Base de Regras (Utilização do Material no SAP):
  0 - Revenda
  1 - Industrialização / Imobilizado Industrial
  2 - Consumo Geral / Demais Operações
  3 - Imobilizado
  4 - Consumo Atividade Principal / Consumo Industrial
"""

import logging
import os

log = logging.getLogger(__name__)

# Tipos de imposto de DIFAL (cadastro de tipos de imposto do SAP, levantado pelo João em 28/10/2026). Não entram
# na conferência de ICMS dedutível: são somados à parte e informados no campo DIFAL do card.
# ICOP ("Complemento do ICMS") é o DIFAL usado aqui (confirmado pelo Contábil em 28/10/2026).
DIFAL_TIPOS = {t.strip().upper() for t in os.getenv("DIFAL_TIPOS", "ICOP,ICML,ICMM,ICMP,ICMT,ICMV,CIDC,CIDF").split(",") if t.strip()}
# Levantamento da CDS (levantar_tipos_imposto.py, 28/10/2026): ISUP ("PIS/COFINS dif. comprador-fornecedor") vem no
# grupo COFI ao lado do ICOF/ICON, com valor. Não é o COFINS da nota: fica fora da soma e da decisão de dedutível.
IGNORAR_TIPOS = {t.strip().upper() for t in os.getenv("POP_IGNORAR_TIPOS", "ISUP").split(",") if t.strip()}
# Não dedutíveis que não terminam em 2/N. CIIC = "ICMS, não dedutível (CIAP)".
NAO_DEDUTIVEIS = {t.strip().upper() for t in os.getenv("POP_NAO_DEDUTIVEIS", "CIIC").split(",") if t.strip()}

def _formatar_imposto(nome: str, dedutivel: bool, val_sap: float, val_ref: float, label_ref: str) -> str:
    """Gera a string comparativa mostrando a decisão de dedutibilidade e se o valor bate (XML/Regra vs SAP)"""
    status = "SIM" if dedutivel else "NÃO"
    
    if dedutivel:
        # Margem de tolerância de 10 centavos para eventuais arredondamentos do SAP
        if abs(val_sap - val_ref) <= 0.10:
            status_valor = "✅ Exato"
        else:
            status_valor = "❌ Incorreto"
            
        return f"Deduziu SAP? {status} | Val. {status_valor} ({label_ref}: R$ {val_ref:.2f} | SAP: R$ {val_sap:.2f})"
    else:
        return f"Deduziu SAP? {status} (Sem direito a crédito)"

def auditar(tipo_doc: str, pop_type: str, uf_empresa: str, uf_emitente: str, itens_po: list, dados_fiscais_cds: list = None) -> str:
    """Relatório de auditoria em texto (compatibilidade). Ver auditar_detalhado."""
    return auditar_detalhado(tipo_doc, pop_type, uf_empresa, uf_emitente, itens_po, dados_fiscais_cds)["texto"]


def auditar_detalhado(tipo_doc: str, pop_type: str, uf_empresa: str, uf_emitente: str, itens_po: list,
                      dados_fiscais_cds: list = None) -> dict:
    """
    Gera o relatório de auditoria fiscal consumindo a Custom CDS View YY1_NF_ITEM_TAX_CDS.
    Devolve {"texto", "status", "itens"}:
      status = "ok" (tudo conferido e certo) | "divergente" (achou erro) | "nao_verificado" (faltou dado para
      conferir algum item -- nunca é tratado como certo; pedido do João, 28/10/2026, para pular a fase Contábil);
      itens = [{"item", "utilizacao", "cfop", "difal", "status", "motivos"}] por item lançado.
    """
    if dados_fiscais_cds is None:
        dados_fiscais_cds = []
        
    if not itens_po:
        return {"texto": "✅ *Auditoria POP:* Avaliação ignorada (Lançamento avulso sem pedido vinculado).",
                "status": "nao_verificado", "itens": []}

    dentro_estado = (uf_empresa.upper() == uf_emitente.upper())
    prefixo_cfop = "1" if dentro_estado else "2"

    relatorio = ["📊 *Auditoria Fiscal Pós-Lançamento (POPs)*\n"]
    falhas = 0
    nao_verificados = 0
    resumo_itens = []

    itens_utilizados = [p for p in itens_po if "xml_item_nf" in p]
    itens_utilizados.sort(key=lambda x: int(x["xml_item_nf"].get("seq", 1)))

    # O item do documento fiscal do SAP é ligado ao item do pedido pela posição (10, 20, 30...). Se o número de
    # itens do documento não bate com o de itens lançados, essa ligação não é confiável: nada é dado como certo.
    itens_doc_sap = sorted({str(i.get("BR_NotaFiscalItem", "")) for i in dados_fiscais_cds if i.get("BR_NotaFiscalItem")})
    motivo_geral_nv = ""
    if not dados_fiscais_cds:
        motivo_geral_nv = "documento fiscal não encontrado no SAP (CDS de impostos vazia)"
    elif len(itens_doc_sap) != len(itens_utilizados):
        motivo_geral_nv = (f"o documento fiscal do SAP tem {len(itens_doc_sap)} item(ns) e foram lançados "
                           f"{len(itens_utilizados)}: não dá para ligar item a item com segurança")
    if not itens_utilizados:
        motivo_geral_nv = motivo_geral_nv or "nenhum item do pedido ligado à NF"
    
    for indice_seq, po_item_dict in enumerate(itens_utilizados):
        
        linha_limpa_sap = str((indice_seq + 1) * 10)
        linha_sap_pad = linha_limpa_sap.zfill(6)
        
        uso_sap = str(po_item_dict.get("material_usage", "")).strip()
        motivos_nv = [motivo_geral_nv] if motivo_geral_nv else []
        difal_item = 0.0

        if not uso_sap:
            relatorio.append(f"⚠️ *Item {linha_limpa_sap}:* Utilização do Material não preenchida no Pedido SAP.\n")
            falhas += 1
            resumo_itens.append({"item": linha_limpa_sap, "utilizacao": "", "cfop": "", "difal": 0.0,
                                 "status": "divergente", "motivos": ["utilização vazia"]})
            continue

        xml_nf_item = po_item_dict.get("xml_item_nf", {})

        # Pega os valores da Pricing API como fallback secundário para segurança
        impostos_sap_pricing = po_item_dict.get("impostos_sap", {})
        val_icms_sap = abs(float(impostos_sap_pricing.get("ZICM", 0.0)))
        val_ipi_sap = abs(float(impostos_sap_pricing.get("ZIPI", 0.0)))
        val_pis_sap = abs(float(impostos_sap_pricing.get("ZPIS", 0.0)))
        val_cof_sap = abs(float(impostos_sap_pricing.get("ZCOF", 0.0)))

        # ---------------------------------------------------------------------
        # EXTRAÇÃO NA CUSTOM CDS VIEW (CFOP, CST E IMPOSTOS FINANCEIROS)
        # ---------------------------------------------------------------------
        cfop_sap = "Não Encontrado"
        cst_icms, cst_ipi, cst_pis, cst_cofins = "", "", "", ""
        
        itens_deste_item = [i for i in dados_fiscais_cds if i.get("BR_NotaFiscalItem", "") == linha_sap_pad]
        
        if not itens_deste_item and dados_fiscais_cds:
            motivos_nv.append(f"item {linha_sap_pad} não encontrado no documento fiscal do SAP")
            primeiro_item_nf = dados_fiscais_cds[0].get("BR_NotaFiscalItem", "")
            itens_deste_item = [i for i in dados_fiscais_cds if i.get("BR_NotaFiscalItem", "") == primeiro_item_nf]

        icms_dedut = False
        ipi_dedut = False
        pis_dedut = False
        cof_dedut = False

        # Valores realmente lançados no documento fiscal do SAP (CDS, BR_NFItemTaxAmount) para ESTE item.
        # O pricing do Pedido é do item inteiro (quantidade pedida) e só bate quando a NF fatura o item
        # todo; fica só como reserva quando a CDS não traz o valor.
        do_item_exato = [i for i in dados_fiscais_cds if i.get("BR_NotaFiscalItem", "") == linha_sap_pad]
        # DIFAL: somado à parte e fora da conferência de ICMS (ver DIFAL_TIPOS)
        for i in do_item_exato:
            if str(i.get("BR_TaxType", "")).strip().upper() in DIFAL_TIPOS:
                try:
                    difal_item += abs(float(i.get("BR_NFItemTaxAmount") or 0))
                except (TypeError, ValueError):
                    pass
        do_item_exato_sem_difal = [i for i in do_item_exato if str(i.get("BR_TaxType", "")).strip().upper() not in DIFAL_TIPOS]
        if do_item_exato and not all("BR_NFItemTaxAmount" in i for i in do_item_exato):
            motivos_nv.append("valores dos impostos não vieram do documento fiscal do SAP")
        if do_item_exato and all("BR_NFItemTaxAmount" in i for i in do_item_exato):
            somas = {}
            for i in do_item_exato_sem_difal:
                if str(i.get("BR_TaxType", "")).strip().upper() in IGNORAR_TIPOS:
                    continue
                g = str(i.get("TaxGroup", "")).strip().upper()
                g = "COFINS" if g == "COFI" else g
                try:
                    somas[g] = somas.get(g, 0.0) + abs(float(i.get("BR_NFItemTaxAmount") or 0))
                except (TypeError, ValueError):
                    pass
            val_icms_sap = somas.get("ICMS", val_icms_sap)
            val_ipi_sap = somas.get("IPI", val_ipi_sap)
            val_pis_sap = somas.get("PIS", val_pis_sap)
            val_cof_sap = somas.get("COFINS", val_cof_sap)

        if itens_deste_item:
            cfop_extraido = str(itens_deste_item[0].get("BR_CFOPCode", "")).strip().replace(".", "")
            if cfop_extraido:
                cfop_sap = cfop_extraido
            else:
                cfop_sap = "OCULTO_API"
                
            cst_icms = str(itens_deste_item[0].get("BR_ICMSTaxSituation", "")).strip()
            cst_ipi = str(itens_deste_item[0].get("BR_IPITaxSituation", "")).strip()
            cst_pis = str(itens_deste_item[0].get("BR_PISTaxSituation", "")).strip()
            cst_cofins = str(itens_deste_item[0].get("BR_COFINSTaxSituation", "")).strip()
            
            # Análise de Dedução sobre a Custom CDS
            for linha_imp in itens_deste_item:
                tax_type = linha_imp.get("BR_TaxType", "").strip().upper()
                if tax_type in DIFAL_TIPOS or tax_type in IGNORAR_TIPOS:
                    continue
                grupo = linha_imp.get("TaxGroup", "").strip().upper() 
                
                if not grupo:
                    if "ICM" in tax_type: grupo = "ICMS"
                    elif "PIS" in tax_type: grupo = "PIS"
                    elif "IPI" in tax_type and "PIS" not in tax_type: grupo = "IPI"
                    elif "COF" in tax_type: grupo = "COFINS"

                is_deductible = not (tax_type.endswith("2") or tax_type.endswith("N") or tax_type in NAO_DEDUTIVEIS)
                # Mais de uma linha no mesmo grupo: dedutível se alguma for (antes valia a última, pela ordem).
                
                if grupo == "ICMS": icms_dedut = icms_dedut or is_deductible
                elif grupo == "IPI": ipi_dedut = ipi_dedut or is_deductible
                elif grupo == "PIS": pis_dedut = pis_dedut or is_deductible
                elif grupo in ["COFI", "COFINS"]: cof_dedut = cof_dedut or is_deductible
            
        # ---------------------------------------------------------------------
        # VALORES DO XML E CÁLCULO DA REGRA MATEMÁTICA
        # ---------------------------------------------------------------------
        if not xml_nf_item:
            motivos_nv.append("linha do XML não ligada ao item")
        val_icms_xml = float(xml_nf_item.get("vICMS", 0.0)) if xml_nf_item else 0.0
        val_ipi_xml = float(xml_nf_item.get("vIPI", 0.0)) if xml_nf_item else 0.0
        valor_cheio_xml = float(xml_nf_item.get("valor", 0.0)) if xml_nf_item else 0.0
        
        base_pis_cofins = max(0.0, valor_cheio_xml - val_icms_xml)
        esp_pis = base_pis_cofins * 0.0165
        esp_cofins = base_pis_cofins * 0.0760

        cfop_esperado = ""
        regra_encontrada = ""
        impostos_esperados = ""
        divergencia_regra = False
        
        # ─────────────────────────────────────────────────────────────
        # 1. POP Notas de serviços (NFS-e)
        # ─────────────────────────────────────────────────────────────
        if tipo_doc == "NFSE":
            cfop_esperado = f"{prefixo_cfop}933"
            if uso_sap == "4":
                regra_encontrada = "Consumo para atividade industrial"
                impostos_esperados = "PIS e Cofins dedutíveis, os demais impostos não dedutíveis."
                if not pis_dedut or not cof_dedut: divergencia_regra = True
            elif uso_sap == "2":
                regra_encontrada = "Demais serviços"
                impostos_esperados = "Impostos não dedutíveis."
                if pis_dedut or cof_dedut: divergencia_regra = True
            else:
                relatorio.append(f"❌ *Item {linha_limpa_sap}:* Utilização '{uso_sap}' inválida para Serviços. O POP permite apenas Uso 4 ou 2.\n")
                falhas += 1
                resumo_itens.append({"item": linha_limpa_sap, "utilizacao": uso_sap, "cfop": "", "difal": difal_item,
                                     "status": "divergente", "motivos": ["utilização inválida para o POP"]})
                continue
                
        # ─────────────────────────────────────────────────────────────
        # 2. POP Notas de frete tipo B (CT-e)
        # ─────────────────────────────────────────────────────────────
        elif tipo_doc == "CTE" and pop_type == "tipo_b":
            cfop_esperado = f"{prefixo_cfop}353"
            if uso_sap == "0":
                regra_encontrada = "Revenda/Venda/Devolução"
                impostos_esperados = "ICMS dedutíveis, os demais impostos não dedutíveis."
                if not icms_dedut or pis_dedut or cof_dedut: divergencia_regra = True
            elif uso_sap == "2":
                regra_encontrada = "Consumo/Demais operações"
                impostos_esperados = "Impostos não dedutíveis."
                if icms_dedut or pis_dedut or cof_dedut: divergencia_regra = True
            else:
                relatorio.append(f"❌ *Item {linha_limpa_sap}:* Utilização '{uso_sap}' inválida para Frete tipo B. O POP permite apenas Revenda/Devolução (0) ou Demais Operações/Consumo (2).\n")
                falhas += 1
                resumo_itens.append({"item": linha_limpa_sap, "utilizacao": uso_sap, "cfop": "", "difal": difal_item,
                                     "status": "divergente", "motivos": ["utilização inválida para o POP"]})
                continue

        # ─────────────────────────────────────────────────────────────
        # 3. POP Notas de frete tipo A (CT-e)
        # ─────────────────────────────────────────────────────────────
        elif tipo_doc == "CTE" and pop_type == "tipo_a":
            cfop_esperado = f"{prefixo_cfop}352"
            if uso_sap in ["0", "1"]:
                regra_encontrada = "Revenda/Produção"
                impostos_esperados = "PIS, Cofins e ICMS dedutíveis, os demais impostos não dedutíveis."
                if not icms_dedut or not pis_dedut or not cof_dedut: divergencia_regra = True
            elif uso_sap == "4":
                regra_encontrada = "Consumo para atividade industrial"
                impostos_esperados = "PIS e Cofins dedutíveis, os demais impostos não dedutíveis."
                if not pis_dedut or not cof_dedut or icms_dedut: divergencia_regra = True
            elif uso_sap in ["2", "3"]:
                regra_encontrada = "Consumo Geral / Imobilizado"
                impostos_esperados = "Sem impostos dedutíveis."
                if icms_dedut or pis_dedut or cof_dedut: divergencia_regra = True
            else:
                relatorio.append(f"❌ *Item {linha_limpa_sap}:* Utilização '{uso_sap}' inválida. O POP de Frete tipo A aceita de 0 a 4.\n")
                falhas += 1
                resumo_itens.append({"item": linha_limpa_sap, "utilizacao": uso_sap, "cfop": "", "difal": difal_item,
                                     "status": "divergente", "motivos": ["utilização inválida para o POP"]})
                continue

        # ─────────────────────────────────────────────────────────────
        # 4. POP Entradas tipo B (NF-e - Mercadoria)
        # ─────────────────────────────────────────────────────────────
        elif tipo_doc == "NFE" and pop_type == "tipo_b":
            if uso_sap == "0":
                regra_encontrada = "Revenda"
                cfop_esperado = f"{prefixo_cfop}102"
                impostos_esperados = "ICMS dedutíveis, os demais impostos não dedutíveis."
                if not icms_dedut or pis_dedut or cof_dedut or ipi_dedut: divergencia_regra = True
            elif uso_sap == "3":
                regra_encontrada = "Imobilizado"
                cfop_esperado = f"{prefixo_cfop}551"
                impostos_esperados = "sem impostos dedutíveis + pode ter DIFAL (fora do estado)"
                if icms_dedut or pis_dedut or cof_dedut or ipi_dedut: divergencia_regra = True
            elif uso_sap == "2":
                regra_encontrada = "Uso/Consumo geral"
                cfop_esperado = f"{prefixo_cfop}556"
                impostos_esperados = "sem impostos dedutíveis + pode ter DIFAL (fora do estado)"
                if icms_dedut or pis_dedut or cof_dedut or ipi_dedut: divergencia_regra = True
            else:
                relatorio.append(f"❌ *Item {linha_limpa_sap}:* Utilização '{uso_sap}' não permitida na filial tipo B. O POP permite apenas Revenda (0), Uso/Consumo (2) ou Imobilizado (3).\n")
                falhas += 1
                resumo_itens.append({"item": linha_limpa_sap, "utilizacao": uso_sap, "cfop": "", "difal": difal_item,
                                     "status": "divergente", "motivos": ["utilização inválida para o POP"]})
                continue

        # ─────────────────────────────────────────────────────────────
        # 5. POP Entradas tipo A (NF-e - Mercadoria)
        # ─────────────────────────────────────────────────────────────
        elif tipo_doc == "NFE" and pop_type == "tipo_a":
            if uso_sap == "0":
                regra_encontrada = "Revenda"
                cfop_esperado = f"{prefixo_cfop}102"
                impostos_esperados = "PIS, Cofins e ICMS dedutíveis, os demais impostos não dedutíveis."
                if not icms_dedut or not pis_dedut or not cof_dedut: divergencia_regra = True
            elif uso_sap == "1":
                ativo_imobilizado = bool(po_item_dict.get("master_fixed_asset"))
                if ativo_imobilizado:
                    regra_encontrada = "Imobilizado Industrial"
                    cfop_esperado = f"{prefixo_cfop}551"
                    impostos_esperados = "CIAP + PIS e Cofins dedutíveis (IPI não dedutível)."
                    if not icms_dedut or not pis_dedut or not cof_dedut or ipi_dedut: 
                        divergencia_regra = True
                else:
                    regra_encontrada = "Industrialização"
                    cfop_esperado = f"{prefixo_cfop}101"
                    impostos_esperados = "PIS, Cofins, ICMS e IPI dedutíveis."
                    tem_ipi_no_xml = float(xml_nf_item.get("vIPI", 0.0)) > 0
                    if not icms_dedut or not pis_dedut or not cof_dedut:
                        divergencia_regra = True
                    elif tem_ipi_no_xml and not ipi_dedut:
                        divergencia_regra = True
            elif uso_sap == "3":
                regra_encontrada = "Imobilizado"
                cfop_esperado = f"{prefixo_cfop}551"
                impostos_esperados = "sem impostos dedutíveis + pode ter DIFAL (fora do estado)"
                if icms_dedut or pis_dedut or cof_dedut or ipi_dedut: divergencia_regra = True
            elif uso_sap == "4":
                regra_encontrada = "Consumo atividade principal"
                cfop_esperado = f"{prefixo_cfop}556"
                impostos_esperados = "PIS e Cofins dedutíveis, os demais impostos não dedutíveis + pode ter DIFAL (fora do estado)"
                if not pis_dedut or not cof_dedut or icms_dedut: divergencia_regra = True
            elif uso_sap == "2":
                regra_encontrada = "Consumo geral"
                cfop_esperado = f"{prefixo_cfop}556"
                impostos_esperados = "sem impostos dedutíveis + pode ter DIFAL (fora do estado)"
                if icms_dedut or pis_dedut or cof_dedut or ipi_dedut: divergencia_regra = True
            else:
                relatorio.append(f"❌ *Item {linha_limpa_sap}:* Utilização '{uso_sap}' inválida no POP de Entradas tipo A.\n")
                falhas += 1
                resumo_itens.append({"item": linha_limpa_sap, "utilizacao": uso_sap, "cfop": "", "difal": difal_item,
                                     "status": "divergente", "motivos": ["utilização inválida para o POP"]})
                continue

            # Override de Centro: 1002 e 1003 (tipo A) não têm aproveitamento de
            # créditos em nenhuma hipótese, mesmo em Utilizações que normalmente
            # dariam direito a crédito (Revenda, Industrialização, Consumo
            # Atividade Principal). CFOP e regra_encontrada permanecem os da
            # Utilização normal -- só a expectativa fiscal muda.
            centro_po = str(po_item_dict.get("plant", "")).strip()
            if uso_sap in ("0", "1", "4") and centro_po in ("1002", "1003"):
                impostos_esperados = f"sem impostos dedutíveis (Centro {centro_po} não possui aproveitamento de créditos, independentemente da Utilização) + pode ter DIFAL (fora do estado)"
                divergencia_regra = icms_dedut or ipi_dedut or pis_dedut or cof_dedut

        # ---------------------------------------------------------------------
        # AVALIAÇÃO DE DIVERGÊNCIAS (REGRA VS VALOR E CFOP)
        # ---------------------------------------------------------------------
        erros_valores = []
        if icms_dedut and abs(val_icms_sap - val_icms_xml) > 0.10: erros_valores.append("ICMS")
        if ipi_dedut and abs(val_ipi_sap - val_ipi_xml) > 0.10: erros_valores.append("IPI")
        if pis_dedut and abs(val_pis_sap - esp_pis) > 0.10: erros_valores.append("PIS")
        if cof_dedut and abs(val_cof_sap - esp_cofins) > 0.10: erros_valores.append("COFINS")

        divergencia_cfop = False
        if cfop_esperado and cfop_sap not in ["Não Encontrado", "OCULTO_API"]:
            cfop_esperado_limpo = cfop_esperado.replace(".", "")
            # Verifica se o CFOP do SAP contém o número base da regra (ex: 1556AA passa se a regra for 1556)
            if not cfop_sap.startswith(cfop_esperado_limpo):
                divergencia_cfop = True

        if cfop_sap in ("Não Encontrado", "OCULTO_API"):
            motivos_nv.append("CFOP ainda não disponível no SAP")
        if not cfop_esperado:
            motivos_nv.append("sem regra de CFOP para este tipo de documento/empresa")

        if not do_item_exato:
            # Sem linhas deste item no documento fiscal do SAP (CDS vazia ou item não encontrado): não dá para dizer
            # que o SAP errou -- fica "não verificado" (vai para o Contábil) em vez de divergência.
            divergencia_regra, divergencia_cfop, erros_valores = False, False, []

        divergencia_total = divergencia_regra or (len(erros_valores) > 0) or divergencia_cfop
        nao_verificado = (not divergencia_total) and bool(motivos_nv)
        icone = "⚠️" if divergencia_total else ("⏳" if nao_verificado else "✅")
        
        alerta_div = ""
        if divergencia_cfop:
            alerta_div += f"\n  ⚠️ *Atenção:* O CFOP aplicado pelo SAP ({cfop_sap}) difere da regra do POP ({cfop_esperado})."
        if divergencia_regra:
            alerta_div += "\n  ⚠️ *Atenção:* O SAP aplicou retenções divergentes da regra do POP para esta Utilização."
        if erros_valores:
            alerta_div += f"\n  ⚠️ *Atenção:* Os valores deduzidos de {', '.join(erros_valores)} estão matematicamente incorretos no SAP."

        if divergencia_total: 
            falhas += 1
        elif nao_verificado:
            nao_verificados += 1
            alerta_div += f"\n  ⏳ *Não verificado:* {'; '.join(dict.fromkeys(motivos_nv))}."
        resumo_itens.append({"item": linha_limpa_sap, "utilizacao": uso_sap,
                             "cfop": cfop_sap if cfop_sap not in ("Não Encontrado", "OCULTO_API") else "",
                             "difal": round(difal_item, 2),
                             "status": "divergente" if divergencia_total else ("nao_verificado" if nao_verificado else "ok"),
                             "motivos": list(dict.fromkeys(motivos_nv))})
            
        status_cfop_str = ""
        if cfop_sap == "Não Encontrado":
            status_cfop_str = "⏳ Delay SAP"
        elif cfop_sap == "OCULTO_API":
            status_cfop_str = "Oculto pela API (Req. CDS View)"
        elif not divergencia_cfop:
            status_cfop_str = "✅ Exato"
        else:
            status_cfop_str = "❌ Incorreto"
            
        label_sap_cfop = cfop_sap if cfop_sap not in ["Não Encontrado", "OCULTO_API"] else "Vazio"

        lbl_icms = f"ICMS (CST {cst_icms})" if cst_icms else "ICMS"
        lbl_ipi  = f"IPI (CST {cst_ipi})" if cst_ipi else "IPI"
        lbl_pis  = f"PIS (CST {cst_pis})" if cst_pis else "PIS"
        lbl_cof  = f"COFINS (CST {cst_cofins})" if cst_cofins else "COFINS"

        status_impostos = (
            f"  • CFOP: {status_cfop_str} (Regra: {cfop_esperado} | SAP: {label_sap_cfop})\n"
            f"  • {lbl_icms}: {_formatar_imposto('ICMS', icms_dedut, val_icms_sap, val_icms_xml, 'XML')}\n"
            f"  • {lbl_ipi}: {_formatar_imposto('IPI', ipi_dedut, val_ipi_sap, val_ipi_xml, 'XML')}\n"
            f"  • {lbl_pis}: {_formatar_imposto('PIS', pis_dedut, val_pis_sap, esp_pis, '1,65%')}\n"
            f"  • {lbl_cof}: {_formatar_imposto('COFINS', cof_dedut, val_cof_sap, esp_cofins, '7,60%')}"
        )
        
        if difal_item > 0.005:
            status_impostos += f"\n  • DIFAL: R$ {difal_item:.2f} (informativo)"
        linha_msg = f"{icone} *Item {linha_limpa_sap} (Uso SAP: {uso_sap} - {regra_encontrada}):* Impostos esperados: {impostos_esperados}\n{status_impostos}{alerta_div}\n"
        relatorio.append(linha_msg)

    if not itens_utilizados and motivo_geral_nv:
        nao_verificados += 1
        relatorio.append(f"⏳ *Não verificado:* {motivo_geral_nv}.\n")

    if falhas:
        status = "divergente"
        relatorio.append("⚠️ *Conclusão:* Lançamento realizado com *divergências* na parametrização do Pedido SAP. Verifique o CFOP, status de dedutibilidade e os valores exatos retidos.")
    elif nao_verificados:
        status = "nao_verificado"
        relatorio.append("⏳ *Conclusão:* Lançamento realizado, mas a auditoria não conseguiu conferir tudo automaticamente (itens marcados acima). Conferência manual necessária.")
    else:
        status = "ok"
        relatorio.append("✅ *Conclusão:* Lançamento realizado. A parametrização do Pedido atende perfeitamente ao CFOP, status de dedutibilidade e aos valores exigidos do Manual POP.")
        
    return {"texto": "\n".join(relatorio), "status": status, "itens": resumo_itens}