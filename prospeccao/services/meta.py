"""
O que conta para a meta de novos leads de UMA pesquisa.

A meta é o total da pesquisa (frentes e municípios somados). Comparar CNPJ é igualdade
do valor normalizado — a IA não participa. Retomada não reclassifica nem conta de novo:
o número é recalculado a partir dos registros desta pesquisa.
"""
from datetime import date

from django.utils import timezone

from carteira.models import CanalEmpresa, ConfiguracaoAnalise, Empresa
from carteira.services.normalizacao import normalizar_texto
from prospeccao.models import Candidato, SelecaoPesquisa, linhas
from prospeccao.services import cruzamento as cz
from prospeccao.services.regioes import expandir

MOTIVOS = {
    "meta": "Meta atingida: leads com CNPJ ativo, cruzados com a carteira e com contato",
    "cota_descoberta": "Candidatas suficientes encontradas; a identificação continua nas próximas execuções",
    "orcamento": "Orçamento atingido",
    "tempo": "Tempo atingido",
    "fontes": "Fontes indisponíveis",
    "consultas": "Consultas planejadas esgotadas",
    "limite_google": "Limite de requisições ao Google atingido",
    "franquia": "Franquia SerpApi incluída esgotada",
    "velocidade": "Limite de velocidade da SerpApi nesta hora; retomada programada",
    "sem_leitura": "Franquia SerpApi não pôde ser lida; nenhuma busca nova",
    "reserva_identidade": "Descoberta interrompida para reservar buscas à identificação",
    "cancelamento": "Cancelamento do usuário",
    "pausa": "Pausa do usuário",
}

CONHECIDAS = (
    cz.Situacao.CLIENTE_CARTEIRA, cz.Situacao.CADASTRO_SEM_COMPRA, cz.Situacao.LEAD_CONHECIDO,
    cz.Situacao.BLOQUEADA, cz.Situacao.NAO_CONTATAR,
)


def municipios(pesquisa):
    nomes, _ = expandir(linhas(pesquisa.cidades))
    return {normalizar_texto(n) for n in nomes}


def situacao_ativa_valida(empresa):
    """ATIVA só com referência dentro da validade. Desconhecida ou falha nunca é ATIVA."""
    if (empresa.situacao_cadastral or "").strip().upper() != "ATIVA":
        return False
    ref = empresa.situacao_cadastral_referencia
    if not ref:
        return False
    dias = ConfiguracaoAnalise.obter().dias_validade_situacao_cadastral
    return (date.today() - ref).days <= int(dias)


def _situacao_canal(canal):
    if canal.confirmacao_humana == CanalEmpresa.Confirmacao.INCORRETO:
        return "Incorreto, conforme verificação da equipe"
    if canal.confirmacao_humana in (CanalEmpresa.Confirmacao.ATENDIDO, CanalEmpresa.Confirmacao.CONFERIDO):
        return "Confirmado pela equipe"
    tec = canal.verificacao_tecnica or {}
    if tec.get("formato") not in (None, "", "ok"):
        return "Formato a conferir"
    if canal.confianca_associacao == "CONFLITO" or canal.nivel in ("CADASTRAL", "PISTA"):
        return "A conferir"
    return "Encontrado na fonte"


def _papel_canal(canal):
    from prospeccao.services.contatos import papel_terceiro
    valor = (canal.valor or "").lower()
    papel = papel_terceiro(canal) or ("Contador" if ("contab" in valor or "contador" in valor) else "")
    return f"{papel}; não é contato direto da empresa" if papel else ""


def _digitos(valor):
    from carteira.services.normalizacao import digitos_telefone
    return digitos_telefone(valor)


def _grafia_ou_novo(itens, vistos, acrescentar, tipo, valor, fonte, situacao="Encontrado na fonte", papel=""):
    """Mesmos dígitos não viram outro contato. A grafia original só substitui o valor já guardado sem trocar a fonte."""
    bruto = (valor or "").strip()
    dig = _digitos(bruto) if tipo == CanalEmpresa.Tipo.TELEFONE else ""
    chave = dig or bruto.lower()
    for item in itens:
        mesmo = item["tipo"] == tipo and ((dig and _digitos(item["valor"]) == dig) or item["valor"].strip().lower() == bruto.lower())
        if not mesmo:
            continue
        if tipo == CanalEmpresa.Tipo.TELEFONE and any(not ch.isdigit() for ch in bruto) and item["valor"].isdigit():
            item["valor"] = bruto
        return
    if (chave, tipo) in vistos:
        return
    acrescentar(tipo, bruto, fonte, situacao, papel)


def listar_contatos(empresa, candidatos=()):
    """Todos os telefones e e-mails, cada um com a fonte. Não escolhe um nem altera o valor."""
    itens, vistos = [], set()

    def acrescentar(tipo, valor, fonte, situacao, papel="", whatsapp=False):
        bruto = (valor or "").strip()
        if not bruto:
            return
        chave = (_digitos(bruto) or bruto.lower(), tipo)
        if chave in vistos:
            return
        vistos.add(chave)
        itens.append({
            "tipo": tipo, "valor": bruto, "fonte": fonte or "", "situacao": situacao,
            "papel": papel, "whatsapp": bool(whatsapp),
            "principal": situacao != "Incorreto, conforme verificação da equipe" and not papel,
        })

    if empresa is not None:
        for canal in empresa.canais.all():
            if canal.tipo not in (CanalEmpresa.Tipo.TELEFONE, CanalEmpresa.Tipo.EMAIL):
                continue
            fonte = canal.fonte or canal.get_origem_canal_display()
            if canal.tipo == CanalEmpresa.Tipo.TELEFONE and canal.whatsapp_explicito:
                fonte = f"{fonte}; WhatsApp informado pela fonte"
            acrescentar(canal.tipo, canal.valor, fonte, _situacao_canal(canal), _papel_canal(canal),
                        canal.tipo == CanalEmpresa.Tipo.TELEFONE and canal.whatsapp_explicito)
        if not any(i["tipo"] == CanalEmpresa.Tipo.TELEFONE for i in itens) and empresa.telefone \
                and "*" not in empresa.telefone:
            acrescentar(CanalEmpresa.Tipo.TELEFONE, empresa.telefone, "cadastro", "A conferir")
        if not any(i["tipo"] == CanalEmpresa.Tipo.EMAIL for i in itens) and empresa.email and "*" not in empresa.email:
            acrescentar(CanalEmpresa.Tipo.EMAIL, empresa.email, "cadastro", "A conferir")
    for cand in candidatos:
        fonte = cand.dominio or (cand.fonte.nome if getattr(cand, "fonte_id", None) else "")
        if cand.telefone:
            _grafia_ou_novo(itens, vistos, acrescentar, CanalEmpresa.Tipo.TELEFONE, cand.telefone, fonte)
        if cand.email and "@" in cand.email and not cand.email.lower().startswith("[email") and "*" not in cand.email:
            situacao = "Formato a conferir" if "contab" in cand.email.lower() else "Encontrado na fonte"
            papel = "Contador ou terceiro; não é contato direto da empresa" if "contab" in cand.email.lower() else ""
            _grafia_ou_novo(itens, vistos, acrescentar, CanalEmpresa.Tipo.EMAIL, cand.email, fonte, situacao, papel)
    itens.sort(key=lambda i: (not i["principal"], i["tipo"], i["valor"]))
    tels = {_digitos(i["valor"]) for i in itens if i["tipo"] == CanalEmpresa.Tipo.TELEFONE and _digitos(i["valor"])}
    emails = {i["valor"].strip().lower() for i in itens if i["tipo"] == CanalEmpresa.Tipo.EMAIL}
    uteis = [i for i in itens if i["principal"]]
    return {
        "itens": itens,
        "divergencia": len(tels) > 1 or len(emails) > 1,
        "com_contato": bool(uteis),
    }


def _potencial_ok(sel):
    """Atividade compatível com a frente. Frota ou oficina próprias não são pré-requisito."""
    if sel.decisao_humana == "RETIRAR":
        return False
    if sel.decisao_humana == "INCLUIR":
        return True
    efetiva = sel.aderencia_humana or sel.aderencia
    if efetiva == SelecaoPesquisa.Aderencia.FORA:
        return False
    texto = " ".join(sel.aderencia_motivos or []).lower()
    if "exclusão explícita" in texto or "exclusao explicita" in texto:
        return False
    if efetiva not in (SelecaoPesquisa.Aderencia.SUSTENTADA, SelecaoPesquisa.Aderencia.PROVAVEL,
                       SelecaoPesquisa.Aderencia.INSUFICIENTE):
        return False
    return bool(sel.aderencia_motivos or sel.motivos_selecao)


def _motivo_comercial(sel):
    motivos = list(sel.aderencia_motivos or []) + list(sel.motivos_selecao or [])
    frase = next((m for m in motivos if m), "")
    efetiva = sel.aderencia_humana or sel.aderencia
    if efetiva == SelecaoPesquisa.Aderencia.INSUFICIENTE and frase:
        return f"Hipótese comercial, sem comprovação de parque próprio: {frase}"
    return frase


def _mensagem_carteira(quando):
    if quando:
        return f"Usando a carteira sincronizada em {timezone.localtime(quando).strftime('%d/%m/%Y %H:%M')}."
    return ("Cruzamento pendente: não há carteira local utilizável. "
            "Estes resultados não estão confirmados como novos.")


def _base(est):
    atual, quando, horas = cz.cruzamento_atual()
    if est is not None and getattr(est, "sync", None):
        atual, quando, horas = est.sync
    conferencia = getattr(est, "conferencia", None) if est is not None else None
    utilizavel = bool(quando) or isinstance(conferencia, dict)
    aviso = getattr(est, "aviso_carteira", None) if est is not None else None
    if not aviso:
        aviso = _mensagem_carteira(quando)
    remotos = set()
    if isinstance(conferencia, dict):
        for cnpj, info in conferencia.items():
            if (info or {}).get("contas") or (info or {}).get("leads"):
                remotos.add(cnpj)
    return utilizavel, aviso, quando, horas, remotos


_DIRETORIOS = (
    "solutudo.", "econodata.", "casadosdados.", "maiscnpj.", "cnpj.biz", "cnpjbiz.", "telelistas.",
    "apontador.", "guiamais.", "guiade", "diretorio", "encontra", "google.", "facebook.", "instagram.",
    "linkedin.", "mercadolivre.", "olx.", "reclameaqui.",
)


def _email_comercial(valor):
    email = (valor or "").strip().lower()
    if "@" not in email or email.startswith("[email") or "protected" in email:
        return ""
    if "contab" in email or "contador" in email:
        return ""
    return email


def _indice_carteira():
    from carteira.services.normalizacao import digitos_telefone
    tels, emails, cnpjs, dominios = set(), set(), set(), set()
    contas = Empresa.objects.filter(salesforce_id__isnull=False)
    for tel, email, cnpj in contas.values_list("telefone", "email", "cnpj"):
        dig = digitos_telefone(tel)
        if dig:
            tels.add(dig)
        if email and "@" in email:
            emails.add(email.strip().lower())
        if cnpj:
            cnpjs.add(cnpj)
    for valor, tipo in CanalEmpresa.objects.filter(empresa__salesforce_id__isnull=False).values_list("valor", "tipo"):
        if tipo == CanalEmpresa.Tipo.TELEFONE:
            dig = digitos_telefone(valor)
            if dig:
                tels.add(dig)
        elif tipo == CanalEmpresa.Tipo.EMAIL and valor and "@" in valor:
            emails.add(valor.strip().lower())
        elif tipo == CanalEmpresa.Tipo.SITE and valor:
            from carteira.services.normalizacao import dominio_de_url
            dom = dominio_de_url(valor)
            if dom:
                dominios.add(dom)
    return tels, emails, cnpjs, dominios


def _na_regiao(empresa, cidades):
    if not cidades:
        return False
    cidade = normalizar_texto(empresa.cidade)
    if not cidade:
        return True  # a seleção já filtrou o município da Receita
    return cidade in cidades


def _nome_base(nome):
    n = normalizar_texto(nome)
    for suf in (" EIRELI", " LTDA", " EPP", " ME", " SA"):
        if n.endswith(suf):
            n = n[: -len(suf)].strip()
    return n


def _montar_lead(nome, cidade, cnpj, motivo, contatos, pendencias, fontes=""):
    return {
        "nome": nome, "cidade": cidade or "", "cnpj": cnpj or "",
        "motivo": motivo, "fonte": fontes, "contatos": contatos["itens"],
        "divergencia": contatos["divergencia"], "com_contato": contatos["com_contato"],
        "pendencias": pendencias,
        "telefone": next((i["valor"] for i in contatos["itens"] if i["tipo"] == CanalEmpresa.Tipo.TELEFONE and i["principal"]), ""),
        "email": next((i["valor"] for i in contatos["itens"] if i["tipo"] == CanalEmpresa.Tipo.EMAIL and i["principal"]), ""),
    }


def _cruzar_sinais(cnpjs, telefones, emails, dominios):
    """Só os identificadores desta pesquisa, contra a carteira local. Não varre a base inteira."""
    cnpjs = {c for c in cnpjs if c}
    telefones = {t for t in telefones if t}
    emails = {e for e in emails if e}
    dominios = {d for d in dominios if d}
    cnpjs_sf, tels_sf, emails_sf = set(), set(), set()
    if cnpjs:
        cnpjs_sf = set(Empresa.objects.filter(cnpj__in=cnpjs, salesforce_id__isnull=False).values_list("cnpj", flat=True))
        from carteira.models import LeadSalesforce
        cnpjs_sf |= set(LeadSalesforce.objects.filter(cnpj__in=cnpjs).values_list("cnpj", flat=True))
    if telefones:
        tels_sf = set(CanalEmpresa.objects.filter(
            empresa__salesforce_id__isnull=False, tipo=CanalEmpresa.Tipo.TELEFONE,
            valor_normalizado__in=telefones).values_list("valor_normalizado", flat=True))
    if emails:
        emails_sf = {e.strip().lower() for e in CanalEmpresa.objects.filter(
            empresa__salesforce_id__isnull=False, tipo=CanalEmpresa.Tipo.EMAIL,
            valor_normalizado__in=emails).values_list("valor_normalizado", flat=True)}
    from carteira.services.normalizacao import dominio_de_url
    dominios_sf = set()
    if dominios:
        for site in CanalEmpresa.objects.filter(
                empresa__salesforce_id__isnull=False, tipo=CanalEmpresa.Tipo.SITE).values_list("valor", flat=True):
            dom = dominio_de_url(site)
            if dom in dominios:
                dominios_sf.add(dom)
    return cnpjs_sf, tels_sf, emails_sf, dominios_sf


def classificar(pesquisa, est=None):
    """Separa novos, conhecidos, repetidos e possíveis correspondências. Não grava e não chama rede."""
    utilizavel, aviso, quando, horas, remotos = _base(est)
    cidades = municipios(pesquisa)
    cache = getattr(est, "cache_cz", None) if est is not None else None
    selecoes = list(
        SelecaoPesquisa.objects.filter(pesquisa=pesquisa).select_related("empresa", "empresa__indicador")
        .prefetch_related("empresa__canais", "empresa__candidatos__fonte"))
    candidatos = list(Candidato.objects.filter(pesquisas=pesquisa).select_related("fonte", "empresa"))
    quadro = {
        "meta": min(int(pesquisa.quantidade_desejada or 0), 100),
        "candidatas": len(candidatos),
        "conhecidas": 0, "repetidas": 0, "pendentes": 0, "reencontradas": 0, "ineditas": 0, "tentativas": 0,
        "acompanhamento": 0,
        "em_analise": 0, "novas_sem_contato": 0, "novos_com_contato": 0, "novos_validados": 0,
        "provisorias": 0, "inativas": 0, "fora": 0, "revisao": 0, "correspondencias": 0,
        "paginas_agrupadas": 0,
        "cruzamento_valido": bool(utilizavel),
        "aviso_carteira": aviso,
        "sincronizacao_em": timezone.localtime(quando).isoformat() if quando else "",
        "sincronizacao_horas": horas,
        "novos": [], "lista_correspondencias": [], "lista_conhecidas": [], "lista_repetidas": [],
        "lista_inativas": [],
    }
    por_empresa = {}
    for cand in candidatos:
        if cand.empresa_id:
            por_empresa.setdefault(cand.empresa_id, []).append(cand)
    nomes_empresa = {}
    cnpjs_vistos, selecoes_vistas, empresas_vistas = set(), set(), set()
    from prospeccao.services.selecao import reencontrada
    from carteira.models import Abordagem

    for sel in selecoes:
        if sel.cnpj in selecoes_vistas:
            quadro["paginas_agrupadas"] += 1
            continue
        selecoes_vistas.add(sel.cnpj)
        empresa = sel.empresa
        if empresa is not None:  # uma ocorrência por empresa na lista, mesmo vinda de várias frentes/consultas
            if empresa.pk in empresas_vistas:
                quadro["paginas_agrupadas"] += 1
                continue
            empresas_vistas.add(empresa.pk)
        ligados = por_empresa.get(empresa.pk, []) if empresa is not None else []
        if sel.fila in (SelecaoPesquisa.Fila.CONHECIDA, SelecaoPesquisa.Fila.NAO_CONTATAR):
            quadro["conhecidas"] += 1
            quadro["lista_conhecidas"].append({"nome": empresa.nome if empresa else sel.cnpj, "cnpj": sel.cnpj, "cidade": empresa.cidade if empresa else ""})
            continue
        if sel.fila in (SelecaoPesquisa.Fila.JA_ABORDADA, SelecaoPesquisa.Fila.RETORNO):
            quadro["acompanhamento"] += 1  # contato efetivo: sai da primeira abordagem, não é apagada
            continue
        if empresa is None:
            continue
        cnpjs_vistos.add(sel.cnpj)
        sit = cz.situacao_empresa(empresa, cache=cache)[0]
        if sit in CONHECIDAS or (empresa.cnpj and empresa.cnpj in remotos):
            quadro["conhecidas"] += 1
            quadro["lista_conhecidas"].append({"nome": empresa.nome, "cnpj": sel.cnpj, "cidade": empresa.cidade})
            continue
        # encontrada anteriormente NÃO é excluída: continua disponível (informação, não bloqueio)
        if not _na_regiao(empresa, cidades) or not _potencial_ok(sel):
            quadro["fora"] += 1
            continue
        contatos = listar_contatos(empresa, ligados)
        pendencias = []
        efetiva = sel.aderencia_humana or sel.aderencia
        if efetiva == SelecaoPesquisa.Aderencia.INSUFICIENTE:
            pendencias.append("Hipótese comercial; a equipe confirma na conversa")
        if contatos["divergencia"]:
            pendencias.append("Contatos diferentes encontrados; conferir na abordagem")
        cadastral = (empresa.situacao_cadastral or "").strip().upper()
        lead = _montar_lead(empresa.nome, empresa.cidade, empresa.cnpj or "", _motivo_comercial(sel), contatos, pendencias)
        if cadastral and cadastral != "ATIVA":
            quadro["inativas"] += 1
            lead["pendencias"] = pendencias + [f"CNPJ {cadastral.lower()}; fora da lista padrão de abordagem"]
            quadro["lista_inativas"].append(lead)
            continue
        if cadastral == "ATIVA" and not situacao_ativa_valida(empresa):
            lead["pendencias"] = pendencias + ["Situação cadastral sem referência válida"]
        lead["identificada"] = bool((empresa.cnpj or "").strip())
        outra = next((d.valor for d in empresa.dados_coletados.all() if d.campo == "cnpj_outra_cidade"), None)
        lead["cnpj_outra_cidade"] = bool(outra)
        if outra:
            onde = ([x.strip() for x in outra.split("|")] + ["", "outro município"])[1]
            lead["pendencias"] = lead["pendencias"] + [f"CNPJ localizado em outra cidade ({onde}); conferir unidade"]
        lead["ativa"] = cadastral == "ATIVA" and situacao_ativa_valida(empresa)
        if not lead["identificada"]:
            lead["pendencias"] = lead["pendencias"] + ["CNPJ ainda não identificado"]
        elif not cadastral:
            lead["pendencias"] = lead["pendencias"] + ["Situação cadastral pendente"]
        conflito = any(c.status == Candidato.Status.CONFLITO for c in ligados) or sel.fila == SelecaoPesquisa.Fila.REVISAR_IDENTIDADE
        if conflito:
            quadro["revisao"] += 1
            quadro["correspondencias"] += 1
            lead["pendencias"] = pendencias + ["Possível correspondência; não é novidade confirmada nem cliente conhecido"]
            quadro["lista_correspondencias"].append(lead)
            continue
        if not utilizavel:
            quadro["provisorias"] += 1
            continue
        nomes_empresa[_nome_base(empresa.nome)] = lead
        lead["id"] = empresa.pk
        lead["reencontrada"] = reencontrada(empresa, pesquisa)
        lead["tentativas"] = Abordagem.objects.filter(empresa=empresa, houve_conversa=False).count()
        quadro["reencontradas" if lead["reencontrada"] else "ineditas"] += 1
        quadro["tentativas"] += bool(lead["tentativas"])
        quadro["novos"].append(lead)
    cnpjs_pagina, tels_pagina, emails_pagina, dominios_pagina = set(), set(), set(), set()
    for cand in candidatos:
        if cand.empresa_id:
            quadro["paginas_agrupadas"] += 1
            continue
        if cand.cnpj_candidato:
            cnpjs_pagina.add(cand.cnpj_candidato)
        dig = _digitos(cand.telefone)
        if dig:
            tels_pagina.add(dig)
        if cand.email and "@" in cand.email:
            emails_pagina.add(cand.email.strip().lower())
        if cand.dominio:
            dominios_pagina.add(cand.dominio.lower())
    cnpjs_sf, tels_sf, emails_sf, dominios_sf = _cruzar_sinais(cnpjs_pagina | cnpjs_vistos, tels_pagina, emails_pagina, dominios_pagina)
    grupos_pagina = {}
    for cand in candidatos:
        if cand.empresa_id or cand.status in (Candidato.Status.EXCLUIDO, Candidato.Status.DESCARTADO):
            continue
        nome = (cand.nome or "").strip()
        base = _nome_base(nome)
        if cand.cnpj_candidato and cand.cnpj_candidato in cnpjs_vistos:
            quadro["paginas_agrupadas"] += 1
            continue
        if cand.cnpj_candidato and cand.cnpj_candidato in cnpjs_sf:
            quadro["conhecidas"] += 1
            quadro["lista_conhecidas"].append({"nome": nome or cand.cnpj_candidato, "cnpj": cand.cnpj_candidato, "cidade": cand.cidade})
            continue
        ref = None
        if base:
            ref = nomes_empresa.get(base)
            if ref is None and len(base) >= 10:
                ref = next((lead for ja, lead in nomes_empresa.items()
                            if ja.startswith(base + " ") or base.startswith(ja + " ")), None)
        if cand.sugestoes or cand.status == Candidato.Status.CONFLITO or ref is not None:
            quadro["revisao"] += 1
            quadro["correspondencias"] += 1
            quadro["lista_correspondencias"].append({
                "nome": nome or cand.dominio or "Página sem nome",
                "cidade": cand.cidade, "cnpj": cand.cnpj_candidato or "",
                "motivo": f"Pode coincidir com {ref['nome']}" if ref else "Identidade parecida com um cadastro já existente",
                "pendencias": ["Possível correspondência; não entra como lead novo nem como cliente conhecido",
                               "CNPJ não localizado" if not cand.cnpj_candidato else "CNPJ citado, vínculo não concluído"],
                "contatos": listar_contatos(None, [cand])["itens"],
                "divergencia": False, "com_contato": bool(cand.telefone or cand.email),
            })
            continue
        dig = _digitos(cand.telefone)
        email = (cand.email or "").strip().lower()
        if (dig and dig in tels_sf) or (email and email in emails_sf) or (cand.dominio and cand.dominio.lower() in dominios_sf):
            quadro["revisao"] += 1
            quadro["correspondencias"] += 1
            quadro["lista_correspondencias"].append({
                "nome": nome or cand.dominio, "cidade": cand.cidade, "cnpj": cand.cnpj_candidato or "",
                "motivo": "Telefone, e-mail ou site também aparece na carteira local",
                "pendencias": ["Possível correspondência com a carteira; conferir antes de tratar como novo"],
                "contatos": listar_contatos(None, [cand])["itens"], "divergencia": False,
                "com_contato": bool(cand.telefone or cand.email),
            })
            continue
        if len(nome) < 4:
            quadro["pendentes"] += 1
            continue
        chave = base or dig or email or cand.dominio or f"id:{cand.pk}"
        grupos_pagina.setdefault(chave, []).append(cand)
    for chave, grupo in grupos_pagina.items():
        primeiro = grupo[0]
        quadro["paginas_agrupadas"] += max(len(grupo) - 1, 0)
        if not utilizavel:
            quadro["provisorias"] += 1
            continue
        atividade = (primeiro.atividade_fonte or "").split(" em ")[0].strip()
        contatos = listar_contatos(None, grupo)
        pendencias = ["CNPJ não localizado", "Não houve conferência por CNPJ"]
        if contatos["divergencia"]:
            pendencias.append("Contatos diferentes encontrados; conferir na abordagem")
        lead_pag = _montar_lead(
            primeiro.nome, primeiro.cidade, "", atividade or "Encontrada pela atividade e pela localidade",
            contatos, pendencias, primeiro.dominio or "")
        lead_pag["identificada"], lead_pag["ativa"] = False, False
        quadro["novos"].append(lead_pag)
        quadro["ineditas"] += 1
    cobertos = set(cnpjs_vistos)
    for item in quadro["novos"] + quadro["lista_correspondencias"] + quadro["lista_conhecidas"] + quadro["lista_repetidas"]:
        if item.get("cnpj"):
            cobertos.add(item["cnpj"])
    for sel in selecoes:
        if sel.empresa_id is None and sel.cnpj not in cobertos:
            quadro["revisao"] += 1
            quadro["correspondencias"] += 1
            quadro["lista_correspondencias"].append({
                "nome": sel.cnpj, "cidade": "", "cnpj": sel.cnpj,
                "motivo": "CNPJ citado na seleção, sem estabelecimento vinculado",
                "pendencias": ["Possível correspondência; não entra como lead novo nem como cliente conhecido"],
                "contatos": [], "divergencia": False, "com_contato": False,
            })
            cobertos.add(sel.cnpj)
    quadro["novos_com_contato"] = sum(1 for item in quadro["novos"] if item["com_contato"])
    quadro["novas_sem_contato"] = sum(1 for item in quadro["novos"] if not item["com_contato"])
    # Funil: candidatas encontradas -> com contato -> identificadas (CNPJ) -> ativas e cruzadas -> leads completos.
    # A META conta só lead completo: CNPJ ativo, cruzado com a carteira (fora dela) e com contato.
    novos = quadro["novos"]
    quadro["novos_encontrados"] = len(novos)
    quadro["identificadas"] = sum(1 for item in novos if item.get("identificada"))
    quadro["ativas_cruzadas"] = sum(1 for item in novos if item.get("ativa"))
    quadro["leads_completos"] = sum(1 for item in novos if item.get("ativa") and item["com_contato"])
    quadro["pendentes_identificacao"] = sum(1 for item in novos if not item.get("identificada"))
    quadro["cnpj_outra_cidade"] = sum(1 for item in novos if item.get("cnpj_outra_cidade"))
    quadro["pendentes_situacao"] = sum(1 for item in novos if item.get("identificada") and not item.get("ativa"))
    quadro["ativas_sem_contato"] = quadro["ativas_cruzadas"] - quadro["leads_completos"]
    quadro["novos_validados"] = quadro["leads_completos"]
    quadro["empresas_unicas"] = (quadro["novos_encontrados"] + quadro["conhecidas"] + quadro["acompanhamento"]
                                 + quadro["correspondencias"] + quadro["inativas"] + quadro["fora"]
                                 + quadro["provisorias"] + quadro["em_analise"])
    return quadro


_LISTAS = ("novos", "lista_correspondencias", "lista_conhecidas", "lista_repetidas", "lista_inativas")


def resumo(pesquisa, est=None):
    """Contagens distintas. Não grava e não chama rede."""
    quadro = classificar(pesquisa, est)
    return {k: v for k, v in quadro.items() if k not in _LISTAS}


def atualizar(execucao, est=None):
    """Grava o painel e o número que a tela trata como novos leads desta pesquisa."""
    pesquisa = execucao.pesquisa
    painel = resumo(pesquisa, est)
    progresso = dict(execucao.progresso or {})
    progresso["painel"] = painel
    if progresso.get("primeiro_resultado_em") is None and (painel["candidatas"] or painel["novos_validados"]):
        progresso["primeiro_resultado_em"] = timezone.now().isoformat()
    execucao.progresso = progresso
    campos = dict(
        progresso=progresso, oportunidades_novas=painel["novos_validados"],
        conhecidos_carteira=painel["conhecidas"], pendentes=painel["pendentes"],
        heartbeat_em=timezone.now())
    type(execucao).objects.filter(pk=execucao.pk).update(**campos)
    for k, v in campos.items():
        if k != "heartbeat_em":
            setattr(execucao, k, v)
    return painel


def definir_motivo(execucao, est, status):
    """Frase de encerramento. Nunca chama o fim da lista de 'mercado esgotado'."""
    from prospeccao.models import Execucao
    motivo = getattr(est, "motivo_fim", "") or ""
    texto = " ".join(getattr(est, "limites", []) or []).lower() + " " + (execucao.mensagem or "").lower()
    painel = (execucao.progresso or {}).get("painel") or {}
    meta_n = painel.get("meta") or execucao.pesquisa.quantidade_desejada
    if motivo == "meta" and painel and painel.get("novos_validados", 0) < (meta_n or 0):
        motivo = ""  # "meta" só quando os LEADS COMPLETOS chegaram à meta
    if motivo in ("", "cota_descoberta", "consultas") and ("tempo máximo" in texto or "tempo maximo" in texto):
        motivo = "tempo"  # o tempo da execução é o que de fato parou a identificação
    if not motivo:
        if status == Execucao.Status.CANCELADA:
            motivo = "cancelamento"
        elif "paus" in texto:
            motivo = "pausa"
        elif "tempo máximo" in texto or "tempo maximo" in texto:
            motivo = "tempo"
        elif "franquia serpapi incluída esgotada" in texto or "franquia serpapi incluida esgotada" in texto:
            motivo = "franquia"
        elif "limite de velocidade" in texto:
            motivo = "velocidade"
        elif "não foi possível ler a franquia" in texto or "nao foi possivel ler a franquia" in texto:
            motivo = "sem_leitura"
        elif any(p in texto for p in ("orçamento", "orcamento", "teto", "sem_orcamento", "sem orçamento")):
            motivo = "orcamento"
        elif any(p in texto for p in ("fonte", "indispon")):
            motivo = "fontes"
        elif status == Execucao.Status.CONCLUIDA:
            painel = (execucao.progresso or {}).get("painel") or {}
            meta = painel.get("meta") or execucao.pesquisa.quantidade_desejada
            motivo = "meta" if painel.get("novos_validados", 0) >= meta else "consultas"
        elif status == Execucao.Status.PARCIAL:
            motivo = "consultas"
    rotulo = MOTIVOS.get(motivo, "")
    progresso = dict(execucao.progresso or {})
    progresso["motivo"] = motivo
    progresso["motivo_rotulo"] = rotulo
    execucao.progresso = progresso
    type(execucao).objects.filter(pk=execucao.pk).update(progresso=progresso)
    if rotulo and rotulo not in (execucao.mensagem or ""):
        execucao.mensagem = (execucao.mensagem + " " if execucao.mensagem else "") + rotulo
        type(execucao).objects.filter(pk=execucao.pk).update(mensagem=execucao.mensagem[:2000])
    return rotulo
