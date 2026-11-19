"""
Identificação do CNPJ sem encerrar só porque já há telefone.

Ordem, parando conforme o que falta e o limite:
  1. textos e respostas já salvos
  2. páginas já lidas do site
  3. no máximo uma busca dirigida (nome + município pesquisado + CNPJ), se houver saldo
  4. consulta cadastral do candidato e novo cruzamento com a carteira

Não inventa dígito, nome, endereço ou domínio. Candidato ambíguo fica registrado e não sobrescreve identidade.
"""
import re

from django.db.models import Q
from django.utils import timezone

from carteira.services.normalizacao import digitos_telefone, normalizar_texto, similaridade_nome
from prospeccao.models import ConsultaWeb
from prospeccao.services.enriquecimento import registrar_dado

_RE_CNPJ = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")


def _dig(valor):
    return re.sub(r"\D", "", valor or "")


def _valido(cnpj):
    from carteira.services.cnpj import calcular_dv
    cnpj = _dig(cnpj)
    return len(cnpj) == 14 and calcular_dv(cnpj[:12]) == cnpj[12:]


def cnpjs_no_texto(texto):
    """(válidos, inválidos_formatados) só do que está escrito. Não completa dígito."""
    validos, invalidos = [], []
    for bruto in _RE_CNPJ.findall(texto or ""):
        cnpj = _dig(bruto)
        if cnpj in validos or cnpj in invalidos:
            continue
        (validos if _valido(cnpj) else invalidos).append(cnpj)
    return validos, invalidos


def marcas_compactas(empresa):
    """Marca curta junta, como BJDIESEL. Palavra genérica sozinha não entra."""
    conectores = {"DE", "DA", "DO", "E", "EM", "LTDA", "ME", "EPP", "EIRELI", "SA"}
    saida = set()
    for nome in (getattr(empresa, "nome", None), getattr(empresa, "razao_social", None),
                 getattr(empresa, "nome_fantasia", None)):
        palavras = [p for p in normalizar_texto(nome or "").split() if p not in conectores]
        curtos = [p for p in palavras if len(p) <= 3]
        compacto = "".join(palavras)
        if curtos and len(compacto) >= 5:
            saida.add(compacto)
    return saida


def tokens_que_identificam(empresa):
    """Palavras do nome desta empresa. Cidade, UF e ramo genérico não identificam."""
    from prospeccao.services.descoberta_google import _GENERICOS_NOME
    from prospeccao.services.identificacao import GENERICAS, tokens_distintivos

    bloqueio = set(_GENERICOS_NOME) | set(GENERICAS)
    bloqueio |= set(normalizar_texto(getattr(empresa, "cidade", "") or "").split())
    bloqueio |= set(normalizar_texto(getattr(empresa, "uf", "") or "").split())
    saida = set()
    for nome in (getattr(empresa, "nome", None), getattr(empresa, "razao_social", None),
                 getattr(empresa, "nome_fantasia", None)):
        saida |= {t for t in tokens_distintivos(nome or "") if len(t) >= 4 and t not in bloqueio}
    return saida


def item_refere_empresa(empresa, item):
    """O resultado cita esta empresa. Dono da consulta, token de cidade ou URL de diretório não bastam."""
    if not isinstance(item, dict) or item.get("diretorio") or item.get("tipo") in ("DIRETORIO", "AI_OVERVIEW"):
        return False
    from prospeccao.conectores.busca_web import eh_diretorio
    from prospeccao.services.descoberta_google import cidade_uf_do_endereco

    dom = item.get("dominio") or ""
    if not dom:
        from carteira.services.normalizacao import dominio_de_url
        dom = dominio_de_url(item.get("url") or item.get("site") or "")
    if item.get("tipo") != "LOCAL" and dom and eh_diretorio(dom):
        return False
    nome = normalizar_texto(f"{item.get('nome_citado') or ''} {item.get('titulo') or ''}")
    palavras = set(nome.split())
    toks = tokens_que_identificam(empresa)
    if not toks or not any(t in palavras for t in toks):
        return False
    cid_item, _uf = cidade_uf_do_endereco(item.get("endereco_citado") or "")
    cid_emp = normalizar_texto(getattr(empresa, "cidade", "") or "")
    if cid_item and cid_emp and cid_item != cid_emp:
        return False
    return True


def _consultas_visiveis(empresa, pesquisa_id):
    """Consultas desta empresa ou da pesquisa. O item ainda precisa citar a empresa."""
    consultas = ConsultaWeb.objects.filter(status="OK")
    if pesquisa_id:
        return consultas.filter(Q(empresa=empresa) | Q(pesquisa_id=pesquisa_id))
    return consultas.filter(empresa=empresa)


def _blob_item(item):
    return " ".join(str(item.get(k) or "") for k in (
        "titulo", "trecho", "nome_citado", "endereco_citado", "cnpj_citado", "atividade", "site"))


def _competencia(carga):
    from datetime import date
    bruto = getattr(carga, "competencia", "") or ""
    try:
        ano, mes = bruto.split("-")
        return date(int(ano), int(mes), 1)
    except (ValueError, AttributeError):
        return None


VERSAO_REGRAS = "[regras-2] "  # marca as decisões tomadas pelas regras de 18/11/2026


_PALAVRAS_VAZIAS = {"DE", "DA", "DO", "DAS", "DOS", "E", "EM", "LTDA", "ME", "EPP", "EIRELI", "SA", "S/A", "CIA"}
_SIGLAS_GENERICAS = {"SP", "ABC", "BR", "ME", "EPP", "SA", "LTDA", "RJ", "MG"}


def _iniciais(texto):
    return "".join(w[0] for w in normalizar_texto(texto).split() if w and w not in _PALAVRAS_VAZIAS)


def _siglas(texto):
    """Siglas do nome: tokens curtos (DBS, WLO, APF) e letras separadas (G&M -> GM, W L O -> WLO)."""
    bruto = normalizar_texto((texto or "").replace("&", " ").replace(".", " "))
    tokens = bruto.split()
    from prospeccao.services.descoberta_google import _GENERICOS_NOME
    # sigla = token curto que não é palavra do ramo (PECAS, AUTO, OLEO... não são sigla)
    saida = {t for t in tokens if 2 <= len(t) <= 4 and t.isalpha() and t not in _SIGLAS_GENERICAS
             and t not in _PALAVRAS_VAZIAS and t not in _GENERICOS_NOME}
    letras = "".join(t for t in tokens if len(t) == 1 and t.isalpha() and t != "E")
    if len(letras) >= 2:
        saida.add(letras)
    junto = re.sub(r"[^A-Z]", "", normalizar_texto((texto or "").split()[0] if texto else ""))
    if 2 <= len(junto) <= 4 and "&" in (texto or "").split()[0]:
        saida.add(junto)
    return saida


def sigla_bate(empresa, dados):
    """Sigla do nome comercial = iniciais/sigla da razão social ou do nome fantasia (G&M = GRELUK E MENEZES)."""
    nome = empresa.nome or ""
    alvos = [getattr(dados, "razao_social", "") or "", getattr(dados, "nome_fantasia", "") or ""]
    s_lead = _siglas(nome)
    ini_lead = _iniciais(nome)
    for alvo in alvos:
        if not alvo:
            continue
        ini_alvo, s_alvo = _iniciais(alvo), _siglas(alvo)
        if any(len(s) >= 2 and (ini_alvo.startswith(s) or s in s_alvo) for s in s_lead):
            return True
        if any(len(s) >= 3 and s == ini_lead for s in s_alvo):  # APF IMPORTS = Auto Peças Fácil
            return True
    return False


def eh_mei(dados):
    """MEI/empresário individual: a razão social começa pelo CNPJ básico ou pelo nome da pessoa."""
    razao = (getattr(dados, "razao_social", "") or "").strip()
    return bool(re.match(r"^\d{2}\.\d{3}\.\d{3}\s", razao))


def telefone_exclusivo(dados):
    """O telefone do cadastro pertence a um só estabelecimento da base carregada (não é contador/escritório)."""
    from prospeccao.conectores.receita import carga_ativa
    from receita.models import EstabelecimentoReceita
    carga = carga_ativa()
    tels = {digitos_telefone(t) for t in (getattr(dados, "telefones", None) or []) if digitos_telefone(t)}
    if carga is None or not tels:
        return False
    n = (EstabelecimentoReceita.objects.filter(carga=carga, telefone1__in=tels).count()
         + EstabelecimentoReceita.objects.filter(carga=carga, telefone2__in=tels).count())
    return n == 1


def nome_proprio_raro(empresa, dados, regiao=False):
    """Uma palavra própria do nome (5+ letras, ex.: BRAJAUTO, MAZETO) está na razão social/fantasia e só UM
    estabelecimento ativo da cidade a tem: identifica mesmo sem telefone ou rua."""
    from prospeccao.conectores.receita import carga_ativa
    from prospeccao.services.descoberta_google import palavras_proprias
    from receita.models import EstabelecimentoReceita
    carga = carga_ativa()
    alvo = normalizar_texto(f"{getattr(dados, 'razao_social', '')} {getattr(dados, 'nome_fantasia', '')}")
    municipio = normalizar_texto(getattr(dados, "municipio", "") or "")
    if carga is None or not municipio:
        return False
    for t in palavras_proprias(empresa.nome):
        if len(t) < 5 or t not in alvo.split():
            continue
        base = EstabelecimentoReceita.objects.filter(carga=carga, situacao_codigo="02")
        if not regiao:
            base = base.filter(municipio=municipio)
        achados = [a for a in base.filter(Q(razao_social__icontains=t) | Q(nome_fantasia__icontains=t))
                   .values_list("razao_social", "nome_fantasia")[:3]
                   if t in normalizar_texto(f"{a[0]} {a[1]}").split()]  # palavra inteira
        if len(achados) == 1:
            return True
    return False


def rua_conflito_forte(rua_emp, rua_cad):
    return False  # endereço do Google x Receita diverge com frequência (mudança, filial); o telefone decide


def avaliar_associacao(empresa, dados):
    """adotar, ambiguo, rejeitar ou pendente_base, mais o motivo.

    Nome comercial pode diferir da razão social. Município do CNPJ diferente do município
    identificado pela evidência de endereço não valida a unidade encontrada.
    """
    if dados is None:
        return "pendente_base", "CNPJ candidato ausente na base local"
    nomes = [empresa.nome or "", empresa.razao_social or "", empresa.nome_fantasia or ""]
    nomes += [n for n in empresa.candidatos.values_list("nome", flat=True) if n]
    alvos = [getattr(dados, "razao_social", "") or "", getattr(dados, "nome_fantasia", "") or ""]
    sim = 0.0
    for nome in nomes:
        for alvo in alvos:
            if nome and alvo:
                sim = max(sim, similaridade_nome(nome, alvo))
    cid_emp = normalizar_texto(empresa.cidade)
    cid_cad = normalizar_texto(getattr(dados, "municipio", "") or "")
    mesmo_mun = bool(cid_emp and cid_cad and cid_emp == cid_cad)
    conflito_mun = bool(cid_emp and cid_cad and cid_emp != cid_cad)
    tels_emp = set(empresa.canais.filter(tipo="TELEFONE").values_list("valor_normalizado", flat=True))
    tels_cad = {digitos_telefone(t) for t in (getattr(dados, "telefones", None) or []) if digitos_telefone(t)}
    def _chave_tel(t):  # DDD + 8 últimos dígitos: a Receita guarda celular antigo sem o 9 inicial
        t = (t or "")[-11:] if len(t or "") > 11 else (t or "")
        return (t[:2], t[-8:]) if len(t) >= 10 else None
    mesmo_tel = bool(tels_emp & tels_cad) or bool(
        {_chave_tel(t) for t in tels_emp if _chave_tel(t)} & {_chave_tel(t) for t in tels_cad if _chave_tel(t)})
    from prospeccao.services.descoberta_google import _rua
    rua_emp, rua_cad = _rua(empresa.endereco), _rua(getattr(dados, "logradouro", "") or "")
    mesma_rua = bool(rua_emp and rua_cad and (rua_emp in rua_cad or rua_cad in rua_emp))
    rua_conflito = bool(rua_emp and rua_cad and not mesma_rua)
    from prospeccao.services.descoberta_google import _GENERICOS_NOME
    tokens = [t for t in normalizar_texto(empresa.nome).split() if t]
    so_categoria = bool(tokens) and all(t in _GENERICOS_NOME or t in {
        "AUTOPECAS", "AUTOPECA", "OFICINA", "TERRAPLENAGEM", "TERRAPLANAGEM", "LOCACAO", "LOCACOES",
    } for t in tokens)
    token_proprio = bool(tokens_que_identificam(empresa) & set(normalizar_texto(
        f"{getattr(dados, 'razao_social', '')} {getattr(dados, 'nome_fantasia', '')}").split()))
    cnpj_txt, mun_txt = getattr(dados, "cnpj", ""), getattr(dados, "municipio", "")
    if conflito_mun:
        nome_ok = not so_categoria and ((token_proprio and sim >= 0.5) or sim >= 0.85
                                        or nome_proprio_raro(empresa, dados, regiao=True))
        if nome_ok:
            # mesma empresa, cadastro em outro município (matriz, filial ou endereço desatualizado na Receita)
            return "outra_cidade", (
                f"CNPJ {cnpj_txt} com o mesmo nome (similaridade {sim:.2f}) está registrado em {mun_txt}; "
                f"a empresa foi encontrada em {empresa.cidade}. Possível matriz, filial ou endereço desatualizado.")
        if sim < 0.34 and not mesmo_tel:
            return "rejeitar", (
                f"CNPJ {cnpj_txt} em {mun_txt} sem correspondência de nome (similaridade {sim:.2f}); "
                "provavelmente do site ou diretório que publicou a empresa")
        return "ambiguo", (
            f"CNPJ {cnpj_txt} está em {mun_txt}, "
            f"diferente do município identificado ({empresa.cidade}). Pode ser outra unidade; não vinculado.")
    if so_categoria and not (mesmo_tel or mesma_rua):
        return "ambiguo", (
            f"o nome informado é só a atividade ({empresa.nome}); CNPJ {cnpj_txt} "
            "não foi vinculado")
    if token_proprio and sim >= 0.5 and mesmo_mun and not rua_conflito:
        return "adotar", f"nome compatível (similaridade {sim:.2f}) e mesmo município do endereço"
    # mais evidência nunca piora: telefone + nome + município juntos vinculam
    obs_rua = "; logradouro diferente do cadastro (possível mudança de endereço)" if rua_conflito else ""
    if mesmo_tel and mesmo_mun and (token_proprio or sim >= 0.6):
        return "adotar", f"telefone em comum, nome compatível (similaridade {sim:.2f}) e mesmo município{obs_rua}"
    if sim >= 0.95 and mesmo_mun and not so_categoria:
        return "adotar", f"nome igual à razão social ou ao nome fantasia ({sim:.2f}) e mesmo município{obs_rua}"
    if mesmo_mun and not so_categoria and nome_proprio_raro(empresa, dados):
        return "adotar", ("nome próprio raro, presente na razão social e único entre as empresas ativas da cidade "
                          f"({getattr(dados, 'razao_social', '')[:60]}); conferir no atendimento")
    if mesmo_tel and mesmo_mun and not so_categoria and not rua_conflito_forte(rua_emp, rua_cad):
        if sigla_bate(empresa, dados):
            return "adotar", ("telefone igual e sigla do nome comercial igual à razão social "
                              f"({getattr(dados, 'razao_social', '')[:60]}); conferir no atendimento")
        if eh_mei(dados) and telefone_exclusivo(dados):
            return "adotar", ("telefone exclusivo de um MEI na mesma cidade (o nome comercial não aparece na Receita); "
                              "conferir no atendimento")
    if mesmo_tel and sim >= 0.4 and mesmo_mun:
        return "ambiguo", (
            f"telefone em comum e nome parecido (similaridade {sim:.2f}) no mesmo município; "
            "possível relação, não é vínculo de CNPJ")
    if token_proprio and sim >= 0.85 and mesmo_mun:
        return "adotar", f"nome muito próximo da razão social ou do nome fantasia ({sim:.2f})"
    if sim >= 0.34 or mesmo_tel or mesma_rua:
        return "ambiguo", (
            f"sinais parciais (nome {sim:.2f}, município {'confirmado' if mesmo_mun else 'sem confirmação'}, "
            f"telefone {'em comum' if mesmo_tel else 'sem correspondência'})")
    return "rejeitar", f"CNPJ citado sem correspondência de nome, endereço ou telefone (similaridade {sim:.2f})"


def _textos(empresa, pesquisa_id):
    """Só o trecho que cita esta empresa. Consulta da pesquisa, telefone ou cidade não puxam CNPJ de outra."""
    vistos = []
    for cw in _consultas_visiveis(empresa, pesquisa_id).iterator():
        for item in cw.resultados or []:
            if not item_refere_empresa(empresa, item):
                continue
            vistos.append((_blob_item(item), item.get("url") or "", f"consulta {cw.pk}"))
    for dado in empresa.dados_coletados.filter(campo__in=["cnpj_publicado_no_site", "complemento_site"]):
        vistos.append((dado.valor or "", dado.referencia or "", dado.fonte or "dado"))
    return vistos


def _marcar_conhecida(empresa, sel, outra, cnpj):
    from prospeccao.models import Oportunidade, SelecaoPesquisa
    from prospeccao.services import cruzamento as cz
    from prospeccao.services import oportunidades as ops
    nota = f"CNPJ {cnpj} já é a conta {outra.salesforce_id} ({outra.nome[:80]}). Histórico mantido."
    registrar_dado(empresa, "cnpj_candidato", cnpj, "cruzamento_salesforce", nota)
    from carteira.models import CnpjAssociado
    CnpjAssociado.objects.get_or_create(empresa=empresa, cnpj=cnpj, defaults={
        "origem": CnpjAssociado.Origem.PESQUISA, "observacao": nota[:300]})
    if sel is not None:
        motivos = list(sel.fila_motivos or [])
        if nota not in motivos:
            motivos.append(nota)
        sel.fila = SelecaoPesquisa.Fila.CONHECIDA
        sel.fila_motivos = motivos
        sel.save(update_fields=["fila", "fila_motivos", "atualizada_em"])
    sit = cz.situacao_empresa(outra)[0]
    op = Oportunidade.objects.filter(empresa=empresa).first()
    if op is not None:
        if sit == cz.Situacao.LEAD_CONHECIDO:
            op.tipo = Oportunidade.Tipo.LEAD_CONHECIDO
        else:
            op.tipo = Oportunidade.Tipo.CADASTRO_SEM_COMPRA
        if sit == cz.Situacao.CLIENTE_CARTEIRA:
            op.status = Oportunidade.Status.JA_CLIENTE
        op.situacao_carteira = sit
        op.cruzamento_detalhe = nota
        op.cruzado_em = timezone.now()
        op.save()
    ops.avaliar(outra)
    return nota


def _aplicar_situacao(empresa, dados, carga, ctx):
    from prospeccao.services.enriquecimento import aplicar_dados_cnpj
    from prospeccao.services.oportunidades import RegraViolada, verificar_situacao_cadastral
    if dados is not None and carga is not None:
        aplicar_dados_cnpj(
            empresa, dados, "receita_dados_abertos", data_referencia=_competencia(carga),
            rotulo_fonte="Receita Federal – base local")
        empresa.refresh_from_db()
        from datetime import date, timedelta
        from carteira.models import ConfiguracaoAnalise
        dias = int(ConfiguracaoAnalise.obter().dias_validade_situacao_cadastral)
        ref = empresa.situacao_cadastral_referencia
        if empresa.situacao_cadastral and ref and ref >= date.today() - timedelta(days=dias):
            return "local", ""
    try:
        verificar_situacao_cadastral(empresa)
        empresa.refresh_from_db()
        return "complementar", ""
    except RegraViolada as exc:
        if ctx is not None:
            ctx.log(f"{empresa.nome[:40]}: situação cadastral pendente ({exc})", "AVISO", "identificacao")
        return "falha", str(exc)
    except Exception as exc:
        registrar_dado(empresa, "verificacao_situacao", f"FALHA: {type(exc).__name__}: {exc}"[:500], "brasilapi_cnpj")
        if ctx is not None:
            ctx.log(f"{empresa.nome[:40]}: consulta cadastral indisponível ({type(exc).__name__})", "AVISO", "identificacao")
        return "falha", type(exc).__name__


def _recruzar(empresa, sel):
    from prospeccao.models import SelecaoPesquisa
    from prospeccao.services import cruzamento as cz
    from prospeccao.services import oportunidades as ops
    ops.avaliar(empresa, getattr(sel, "pesquisa", None))
    empresa.refresh_from_db()
    sit = cz.situacao_empresa(empresa)[0]
    if sel is None or sit not in (
            cz.Situacao.CLIENTE_CARTEIRA, cz.Situacao.CADASTRO_SEM_COMPRA, cz.Situacao.LEAD_CONHECIDO,
            cz.Situacao.BLOQUEADA, cz.Situacao.NAO_CONTATAR):
        return
    quando = cz.ultima_sincronizacao_clientes()
    nota = "Conhecida na carteira após identificar o CNPJ" + (
        f" (carteira de {timezone.localtime(quando):%d/%m/%Y %H:%M})" if quando else "")
    motivos = list(sel.fila_motivos or [])
    if nota not in motivos:
        motivos.append(nota)
    sel.fila = SelecaoPesquisa.Fila.CONHECIDA
    sel.fila_motivos = motivos
    sel.save(update_fields=["fila", "fila_motivos", "atualizada_em"])


_consulta_cadastral_liberada = True



def filial_local(empresa, cnpj):
    """Estabelecimento da MESMA RAIZ no município onde a empresa foi encontrada (base local da Receita, sem custo)."""
    from prospeccao.conectores.receita import carga_ativa, estab_para_dados
    from receita.importador import tabela
    from receita.models import EstabelecimentoReceita
    carga = carga_ativa()
    alvo = normalizar_texto(empresa.cidade)
    if not carga or not cnpj or not alvo:
        return None, None
    codigos = [c for c, d in tabela(carga.competencia, "MUNICIPIO").items() if normalizar_texto(d) == alvo]
    if not codigos:
        return None, None
    e = (EstabelecimentoReceita.objects.filter(carga=carga, cnpj_basico=cnpj[:8], municipio_codigo__in=codigos)
         .exclude(cnpj=cnpj).order_by("situacao_codigo").first())
    return (estab_para_dados(e, carga), carga) if e else (None, None)


def resolver_candidato(empresa, sel, cnpj, dados, carga, ctx, origem, permitir_consulta_complementar=True):
    """Aplica a decisão sobre UM CNPJ candidato. Retorna info terminal, ou None para seguir com o próximo candidato.

    - adotar: vincula, aplica situação e recruza;
    - CNPJ já é conta do Salesforce (o próprio ou a mesma raiz, quando é de outra cidade): fila CONHECIDA;
    - outra_cidade: procura a filial da mesma raiz no município; sem filial, vincula o CNPJ encontrado
      e registra "CNPJ localizado em outra cidade" (o município da empresa encontrada é preservado);
    - ambiguo/rejeitar: registra o motivo."""
    from carteira.models import Empresa
    from prospeccao.services import oportunidades as ops
    from prospeccao.services.descoberta_google import adotar_cnpj
    info = {"buscas_novas": 0, "consultas_novas": [], "terminal": True}
    decisao, motivo = avaliar_associacao(empresa, dados)
    outra_cidade = None
    if decisao == "outra_cidade":
        filial, carga_f = filial_local(empresa, cnpj)
        if filial is not None and avaliar_associacao(empresa, filial)[0] == "adotar":
            outra_cidade = None
            motivo = f"filial da mesma raiz no município ({filial.cnpj}); o CNPJ citado {cnpj} é de {dados.municipio}"
            cnpj, dados, carga = filial.cnpj, filial, carga_f
        else:
            outra_cidade = f"{cnpj} | {dados.municipio}/{getattr(dados, 'uf', '')} | {dados.situacao_cadastral or '-'}"
            raiz_sf = Empresa.objects.filter(cnpj__startswith=cnpj[:8], salesforce_id__isnull=False)                 .exclude(pk=empresa.pk).first()
            if raiz_sf is not None:
                nota = _marcar_conhecida(empresa, sel, raiz_sf, cnpj)
                info.update(motivo=f"Unidade local de conta já na carteira (mesma raiz de CNPJ). {nota}",
                            acao="conhecida_salesforce")
                registrar_dado(empresa, "motivo_encerramento_identificacao", info["motivo"], "identificacao", "")
                return info
        decisao = "adotar"
    if decisao == "adotar" and not empresa.cnpj:
        local = (empresa.cidade, empresa.uf, empresa.endereco, empresa.cep)
        outra, efeito = adotar_cnpj(empresa, sel, cnpj, ctx, f"{motivo}; {origem}")
        if outra is not None and getattr(outra, "salesforce_id", None):
            nota = _marcar_conhecida(empresa, sel, outra, cnpj)
            info.update(motivo=nota, acao="conhecida_salesforce")
            registrar_dado(empresa, "motivo_encerramento_identificacao", info["motivo"], "identificacao", "")
            return info
        if outra is not None:
            registrar_dado(empresa, "cnpj_candidato", cnpj, origem[:60], efeito)
            return None
        empresa.refresh_from_db()
        if not empresa.situacao_cadastral and permitir_consulta_complementar:
            _aplicar_situacao(empresa, dados, carga, ctx)
        elif not empresa.situacao_cadastral and dados is not None:
            from prospeccao.services.enriquecimento import aplicar_dados_cnpj
            aplicar_dados_cnpj(empresa, dados, "receita_dados_abertos", data_referencia=_competencia(carga),
                               rotulo_fonte="Receita Federal – base local")
        empresa.refresh_from_db()
        if outra_cidade:
            # o cadastro é de outro município: a empresa continua onde a pesquisa a encontrou
            empresa.cidade, empresa.uf, empresa.endereco, empresa.cep = local
            empresa.save(update_fields=["cidade", "uf", "endereco", "cep", "atualizado_em"])
            registrar_dado(empresa, "cnpj_outra_cidade", outra_cidade, origem[:60], motivo[:500])
        ops.avaliar(empresa, getattr(sel, "pesquisa", None))
        _recruzar(empresa, sel)
        empresa.refresh_from_db()
        if outra_cidade:
            info.update(motivo=f"CNPJ localizado em outra cidade: {motivo}", acao="cnpj_outra_cidade")
        else:
            info.update(motivo=f"CNPJ {cnpj} vinculado: {motivo}", acao="cnpj_vinculado")
        registrar_dado(empresa, "motivo_encerramento_identificacao", info["motivo"], "identificacao", "")
        return info
    if decisao == "ambiguo":
        campo = "cnpj_outra_unidade" if "diferente do município" in motivo else "cnpj_candidato"
        registrar_dado(empresa, campo, cnpj, origem[:60], motivo[:500])
        info.update(motivo=f"Identidade a conferir: {motivo}", acao="ambiguo")
        return info
    if decisao == "rejeitar":
        registrar_dado(empresa, "cnpj_candidato_rejeitado", cnpj, origem[:60], (VERSAO_REGRAS + motivo)[:500])
    return None



def identificar_pela_base(empresa, sel, ctx=None, permitir_consulta_complementar=True):
    """Cruzamento com a base pública da Receita carregada (sem custo, sem Google): telefone + nome, nome + rua,
    nome único/raro. Testa o candidato do TELEFONE e o do NOME separadamente (o telefone do Google pode ser de outra
    empresa) e fica com o que avaliar_associacao aprova. Retorna info (vinculado, outra cidade, conhecida, ambíguo)
    ou None."""
    from prospeccao.conectores.receita import carga_ativa, consultar_local
    from prospeccao.services.descoberta_google import identificar_na_receita
    carga = carga_ativa()
    if carga is None:
        return None
    from receita.importador import tabela
    alvo = normalizar_texto(empresa.cidade)
    cods = [c for c, d in tabela(carga.competencia, "MUNICIPIO").items() if normalizar_texto(d) == alvo] if alvo else []
    tels = [t for t in empresa.canais.filter(tipo="TELEFONE").values_list("valor_normalizado", flat=True) if t]
    nomes = [empresa.nome] + [n for n in empresa.candidatos.values_list("nome", flat=True) if n and n != empresa.nome][:3]
    candidatos = []  # (cnpj, evidência), na ordem: telefone+nome, depois só nome
    for nome in nomes:
        for fones in (tels, []):
            cnpj, evid = identificar_na_receita(carga, None, nome, fones, empresa.endereco,
                                                municipios_nome=cods or None)
            if not cnpj:
                m = re.search(r"possível CNPJ (\d{14})", evid or "")
                cnpj = m.group(1) if m else None
            if cnpj and cnpj not in {c for c, _ in candidatos}:
                candidatos.append((cnpj, evid))
    avaliados = []
    for cnpj, evid in candidatos:
        dados, carga_l = consultar_local(cnpj)
        if dados is None:
            continue
        decisao, _motivo = avaliar_associacao(empresa, dados)
        avaliados.append((decisao, cnpj, evid, dados, carga_l))
    ordem = {"adotar": 0, "outra_cidade": 1, "ambiguo": 2}
    avaliados = [a for a in avaliados if a[0] in ordem]
    avaliados.sort(key=lambda a: ordem[a[0]])
    for decisao, cnpj, evid, dados, carga_l in avaliados:  # se o 1º já é de outro cadastro, tenta o próximo
        r = resolver_candidato(empresa, sel, cnpj, dados, carga_l, ctx, f"base Receita: {evid}"[:60],
                               permitir_consulta_complementar)
        if r is not None:
            registrar_dado(empresa, "identificacao_base_receita", cnpj, "receita_dados_abertos", (evid or "")[:500])
            return r
    registrar_dado(empresa, "identificacao_base_receita", "", "receita_dados_abertos",
                   f"sem correspondência na base carregada ({carga.competencia}) por telefone, nome ou rua")
    return None

def consolidar_sites(empresa):
    """Um site principal por domínio e caminho. A outra URL (http/https) fica como origem."""
    from urllib.parse import urlsplit

    from carteira.models import CanalEmpresa
    from carteira.services.normalizacao import dominio_de_url

    grupos = {}
    for canal in list(empresa.canais.filter(tipo=CanalEmpresa.Tipo.SITE)):
        bruto = canal.valor if "://" in (canal.valor or "") else "http://" + (canal.valor or "")
        partes = urlsplit(bruto)
        chave = (dominio_de_url(canal.valor), (partes.path or "/").rstrip("/").lower())
        grupos.setdefault(chave, []).append(canal)
    ordem = {"ALTA": 0, "MEDIA": 1, "BAIXA": 2, "CONFLITO": 3}
    for grupo in grupos.values():
        if len(grupo) < 2:
            continue
        principal = sorted(grupo, key=lambda c: (
            0 if (c.valor or "").lower().startswith("https") else 1,
            ordem.get(c.confianca_associacao, 9)))[0]
        extras = list(principal.corroboracoes or [])
        for outro in grupo:
            if outro.pk == principal.pk:
                continue
            extras.append({
                "fonte": outro.fonte, "url": outro.valor, "dominio": dominio_de_url(outro.valor),
                "origem": outro.origem_canal, "confianca": outro.confianca_associacao,
                "evidencia": "mesmo site em outro protocolo",
            })
            outro.delete()
        principal.corroboracoes = extras
        principal.save(update_fields=["corroboracoes"])


def recuperar_contatos_salvos(empresa, pesquisa_id=None):
    """Grava contato só do resultado que cita esta empresa. Diretório e homônima de outra cidade ficam de fora."""
    from carteira.models import CanalEmpresa
    from prospeccao.services import contatos as ct

    vistos = set()
    aceitos = rejeitados = 0
    for cw in _consultas_visiveis(empresa, pesquisa_id).iterator():
        for indice, item in enumerate(cw.resultados or []):
            if not isinstance(item, dict):
                continue
            tels = list(item.get("telefones") or [])
            emails = list(item.get("emails") or [])
            if not tels and not emails:
                continue
            if not item_refere_empresa(empresa, item):
                rejeitados += len(tels) + len(emails)
                continue
            chave = (cw.pk, indice, item.get("url") or item.get("titulo"))
            if chave in vistos:
                continue
            vistos.add(chave)
            url = item.get("url") or ""
            evid = (f"consulta {cw.pk} item {indice}: {item.get('nome_citado') or item.get('titulo') or ''} — "
                    f"{item.get('verificacao') or 'trecho salvo'}")[:300]
            for tel in tels:
                if ct.registrar(empresa, CanalEmpresa.Tipo.TELEFONE, tel, fonte="resultado_salvo", url=url,
                                origem=CanalEmpresa.Origem.PUBLICACAO, confianca=CanalEmpresa.Confianca.MEDIA
                                if item.get("tipo") == "LOCAL" else CanalEmpresa.Confianca.BAIXA,
                                aplica_a=CanalEmpresa.AplicaA.EMPRESA if item.get("tipo") == "LOCAL"
                                else CanalEmpresa.AplicaA.DESCONHECIDO, evidencia=evid):
                    aceitos += 1
            for email in emails:
                if ct.registrar(empresa, CanalEmpresa.Tipo.EMAIL, email, fonte="resultado_salvo", url=url,
                                origem=CanalEmpresa.Origem.PUBLICACAO, confianca=CanalEmpresa.Confianca.BAIXA,
                                aplica_a=CanalEmpresa.AplicaA.DESCONHECIDO, evidencia=evid):
                    aceitos += 1
    if aceitos or rejeitados:
        registrar_dado(
            empresa, "extracao_contatos",
            f"aceitos={aceitos}; rejeitados_sem_vinculo={rejeitados}",
            "revisao_vinculo", "")
    return {"aceitos": aceitos, "rejeitados": rejeitados}


def _cnpj_unico_do_site(empresa):
    """CNPJ único publicado no site cujo domínio confirma o nome. Nome só de atividade não confirma."""
    from carteira.services.normalizacao import dominio_de_url

    dado = empresa.dados_coletados.filter(campo="cnpj_publicado_no_site").first()
    if dado is None:
        return None
    validos, _ = cnpjs_no_texto(dado.valor or "")
    if len(validos) != 1:
        return None
    cnpj = validos[0]
    if empresa.dados_coletados.filter(
            campo__in=["cnpj_candidato_rejeitado", "cnpj_outra_unidade"], valor=cnpj).exists():
        return None
    comp = empresa.dados_coletados.filter(campo="complemento_site").first()
    if comp is not None and str(comp.valor).startswith("NAO_RELACIONADO"):
        return None
    toks = tokens_que_identificam(empresa) | marcas_compactas(empresa)
    urls = [dado.referencia or ""]
    urls += list(empresa.canais.filter(tipo="SITE").values_list("valor", flat=True))
    dom = normalizar_texto(" ".join(dominio_de_url(u) for u in urls if u)).replace(" ", "")
    associado = False
    ref_dom = dominio_de_url(dado.referencia or "")
    if ref_dom:
        associado = empresa.canais.filter(
            tipo="SITE", confianca_associacao__in=["ALTA", "MEDIA"], valor__icontains=ref_dom).exists()
    if not any(t in dom for t in toks) and not associado:
        return None
    return cnpj


def _cadastro_confirma(empresa, dados):
    from prospeccao.services.descoberta_google import _rua
    from prospeccao.services.identificacao import tokens_distintivos

    if dados is None:
        return False, "sem cadastro"
    toks = [t for t in tokens_distintivos(empresa.nome) if len(t) >= 4]
    alvo = normalizar_texto(f"{getattr(dados, 'razao_social', '')} {getattr(dados, 'nome_fantasia', '')}")
    nome_ok = bool(toks) and any(t in alvo for t in toks)
    marca_ok = any(m in alvo.replace(" ", "") for m in marcas_compactas(empresa))
    cid_emp = normalizar_texto(empresa.cidade)
    cid_cad = normalizar_texto(getattr(dados, "municipio", "") or "")
    if cid_emp and cid_cad and cid_emp != cid_cad:
        return False, "municipio"
    cid_ok = bool(cid_emp and cid_cad and cid_emp == cid_cad)
    rua_emp, rua_cad = _rua(empresa.endereco), _rua(getattr(dados, "logradouro", "") or "")
    rua_ok = bool(rua_emp and rua_cad and (rua_emp in rua_cad or rua_cad in rua_emp))
    rua_conflito = bool(rua_emp and rua_cad and not rua_ok)
    if nome_ok and cid_ok:
        return True, "nome comercial e município do cadastro"
    if marca_ok and cid_ok and not rua_conflito:
        return True, "marca e município do cadastro, sem conflito de endereço"
    if rua_ok and cid_ok:
        return True, "endereço e município do cadastro"
    return False, "insuficiente"


def _consultar_cadastral(cnpj, empresa=None):
    global _consulta_cadastral_liberada
    if not _consulta_cadastral_liberada:
        return "bloqueada", None
    from prospeccao import conectores
    from prospeccao.conectores.base import ContextoExecucao, FonteIndisponivel
    from prospeccao.models import Fonte

    fonte = Fonte.objects.filter(codigo="brasilapi_cnpj", habilitada=True).first()
    if fonte is None:
        return "sem_fonte", None
    try:
        dados = conectores.ENRIQUECIMENTO["brasilapi_cnpj"](fonte).consultar(cnpj, ContextoExecucao())
    except FonteIndisponivel as exc:
        if "429" in str(exc):
            _consulta_cadastral_liberada = False
            from prospeccao.services.retomada import agendar
            agendar("brasilapi_cnpj", cnpj, str(exc), getattr(exc, "retry_after", None), empresa=empresa)
        return "indisponivel", str(exc)[:300]
    except Exception as exc:
        return "falha", f"{type(exc).__name__}: {exc}"[:300]
    if dados is None:
        return "sem_retorno", None
    return "ok", dados


def aproveitar_cnpj_publicado(empresa, sel, consultar=True):
    """Associa o CNPJ do site próprio quando o cadastro confirma nome ou endereço no mesmo município."""
    if empresa.cnpj:
        return None
    cnpj = _cnpj_unico_do_site(empresa)
    if not cnpj:
        return None
    from prospeccao.conectores.receita import consultar_local
    from prospeccao.services.descoberta_google import adotar_cnpj
    from prospeccao.services.enriquecimento import aplicar_dados_cnpj
    from prospeccao.services import oportunidades as ops

    local, carga = consultar_local(cnpj)
    confirma, porque = _cadastro_confirma(empresa, local)
    if local is not None and porque == "municipio":
        registrar_dado(empresa, "cnpj_outra_unidade", cnpj, "site_publico",
                       "CNPJ do site está em outro município. Não vinculado.")
        return None
    dados_api = None
    sem_cadastro = False
    if local is None and consultar:
        prev = empresa.dados_coletados.filter(campo="proxima_acao", fonte="identificacao").first()
        texto_prev = (prev.valor or "") if prev else ""
        if "não devolveu o registro" in texto_prev:
            estado, payload = "sem_retorno", None
        elif "429" in texto_prev or "indisponível" in texto_prev:
            estado, payload = "indisponivel", texto_prev
        else:
            estado, payload = _consultar_cadastral(cnpj, empresa)
        if estado == "ok":
            dados_api = payload
            confirma, porque = _cadastro_confirma(empresa, dados_api)
            if porque == "municipio":
                registrar_dado(empresa, "cnpj_outra_unidade", cnpj, "brasilapi_cnpj",
                               "Cadastro do CNPJ publicado no site está em outro município. Não vinculado.")
                return None
        else:
            # Ausência ou limite da fonte cadastral não apaga o CNPJ publicado no site próprio.
            sem_cadastro = True
            confirma = True
            if estado == "sem_retorno":
                porque = "CNPJ único publicado no site próprio; a consulta cadastral não devolveu o registro"
            else:
                porque = "CNPJ único publicado no site próprio; consulta cadastral indisponível, não é inexistente"
            registrar_dado(empresa, "proxima_acao", porque + ". Situação cadastral pendente. " + (payload or ""),
                           "identificacao", "")
    if not confirma:
        registrar_dado(empresa, "cnpj_candidato", cnpj, "site_publico",
                       "CNPJ publicado no site; nome ou endereço ainda não confirmam a unidade.")
        return None
    outra, _efeito = adotar_cnpj(empresa, sel, cnpj, None, f"site próprio: {porque}")
    if outra is not None and getattr(outra, "salesforce_id", None):
        nota = _marcar_conhecida(empresa, sel, outra, cnpj)
        return {"motivo": nota, "terminal": True, "acao": "conhecida_salesforce", "buscas_novas": 0}
    if outra is not None:
        registrar_dado(empresa, "cnpj_candidato", cnpj, "site_publico", _efeito)
        return None
    empresa.refresh_from_db()
    if dados_api is not None and not empresa.situacao_cadastral:
        aplicar_dados_cnpj(empresa, dados_api, "brasilapi_cnpj", data_referencia=timezone.localdate(),
                           rotulo_fonte="BrasilAPI (consulta)")
    elif local is not None and not empresa.situacao_cadastral:
        if consultar:
            _aplicar_situacao(empresa, local, carga, None)
        else:
            from prospeccao.services.enriquecimento import aplicar_dados_cnpj
            aplicar_dados_cnpj(
                empresa, local, "receita_dados_abertos", data_referencia=_competencia(carga),
                rotulo_fonte="Receita Federal – base local")
    elif local is not None and consultar:
        estado, payload = _consultar_cadastral(cnpj, empresa)
        if estado == "ok" and _cadastro_confirma(empresa, payload)[0]:
            aplicar_dados_cnpj(empresa, payload, "brasilapi_cnpj", data_referencia=timezone.localdate(),
                               rotulo_fonte="BrasilAPI (consulta)")
        elif estado in ("indisponivel", "bloqueada", "falha"):
            registrar_dado(empresa, "verificacao_situacao",
                           "Situação da base local preservada. Consulta atual indisponível; não é confirmação nova. "
                           + (payload or estado), "brasilapi_cnpj")
    ops.avaliar(empresa, getattr(sel, "pesquisa", None))
    _recruzar(empresa, sel)
    empresa.refresh_from_db()
    motivo = f"CNPJ {cnpj} vinculado pelo site próprio: {porque}"
    registrar_dado(empresa, "motivo_encerramento_identificacao", motivo, "identificacao", "")
    return {"motivo": motivo, "terminal": True, "acao": "cnpj_vinculado", "buscas_novas": 0, "consultas_novas": []}


_FONTES_CANDIDATO_PRESERVADAS = ("site_publico", "receita", "brasilapi", "cruzamento", "revisao_humana")


def _cnpj_no_item(item, cnpj):
    return cnpj in _dig(_blob_item(item))


def cnpj_sustentado(empresa, cnpj, pesquisa_id=None):
    """Há trecho salvo que cita esta empresa e contém o CNPJ, ou o próprio site o publicou."""
    if empresa.dados_coletados.filter(campo="cnpj_publicado_no_site", valor__contains=cnpj).exists():
        return True
    for cw in _consultas_visiveis(empresa, pesquisa_id).iterator():
        for item in cw.resultados or []:
            if item_refere_empresa(empresa, item) and _cnpj_no_item(item, cnpj):
                return True
    return False


def revisar_associacoes(empresa, pesquisa_id=None, preservar_conhecida=False):
    """Invalida candidato ou contato sem trecho desta empresa. Não apaga a origem nem confirmação humana."""
    from django.db import IntegrityError, transaction

    from carteira.models import CanalEmpresa
    from carteira.services.normalizacao import digitos_telefone

    invalidos = preservados = 0
    if not preservar_conhecida:
        for dado in list(empresa.dados_coletados.filter(campo__in=["cnpj_candidato", "cnpj_outra_unidade"])):
            cnpj = _dig(dado.valor)
            if len(cnpj) != 14:
                continue
            fonte = (dado.fonte or "").lower()
            if fonte.startswith(_FONTES_CANDIDATO_PRESERVADAS) or cnpj_sustentado(empresa, cnpj, pesquisa_id):
                preservados += 1
                continue
            motivo = ("CNPJ sem trecho que cite esta empresa. Cidade da consulta, telefone compartilhado "
                      "ou página de outra empresa não mantêm o candidato.")
            nota = f"{motivo} Fonte original: {dado.fonte}. {dado.referencia or ''}"[:500]
            dado.campo = "cnpj_candidato_rejeitado"
            dado.referencia = nota
            try:
                with transaction.atomic():
                    dado.save(update_fields=["campo", "referencia", "coletado_em"])
                invalidos += 1
            except IntegrityError:
                existente = empresa.dados_coletados.filter(
                    campo="cnpj_candidato_rejeitado", fonte=dado.fonte).first()
                if existente is not None and _dig(existente.valor) == cnpj:
                    type(dado).objects.filter(pk=dado.pk).delete()
                else:
                    dado.fonte = (f"{(dado.fonte or 'fonte')[:44]}:{cnpj}")[:60]
                    dado.campo = "cnpj_candidato_rejeitado"
                    dado.referencia = nota
                    dado.save(update_fields=["campo", "fonte", "referencia", "coletado_em"])
                invalidos += 1
    telefones_desta = set()
    telefones_alheia = set()
    for cw in _consultas_visiveis(empresa, pesquisa_id).iterator():
        for item in cw.resultados or []:
            if not isinstance(item, dict):
                continue
            dest = telefones_desta if item_refere_empresa(empresa, item) else telefones_alheia
            for tel in item.get("telefones") or []:
                d = digitos_telefone(str(tel))
                if d:
                    dest.add(d)
            for email in item.get("emails") or []:
                dest.add((email or "").strip().lower())
    for canal in empresa.canais.filter(tipo__in=[CanalEmpresa.Tipo.TELEFONE, CanalEmpresa.Tipo.EMAIL],
                                       origem_canal=CanalEmpresa.Origem.PUBLICACAO):
        if canal.confirmacao_humana in (CanalEmpresa.Confirmacao.ATENDIDO, CanalEmpresa.Confirmacao.CONFERIDO,
                                        CanalEmpresa.Confirmacao.INCORRETO):
            preservados += 1
            continue
        if (canal.fonte or "").startswith("site_publico") or canal.origem_canal == CanalEmpresa.Origem.HUMANO:
            preservados += 1
            continue
        chave = canal.valor_normalizado or canal.valor
        if canal.tipo == CanalEmpresa.Tipo.EMAIL:
            chave = (chave or "").lower()
        if chave in telefones_desta or chave not in telefones_alheia:
            preservados += 1
            continue
        if (canal.evidencia_associacao or "").startswith("associação recusada"):
            invalidos += 1
            continue
        canal.confianca_associacao = CanalEmpresa.Confianca.CONFLITO
        canal.aplica_a = CanalEmpresa.AplicaA.DESCONHECIDO
        canal.evidencia_associacao = (
            "associação recusada: o valor aparece em resultado de outra empresa e não no trecho desta. "
            f"Origem {canal.fonte} mantida.")[:500]
        canal.save(update_fields=["confianca_associacao", "aplica_a", "evidencia_associacao", "coletado_em"])
        invalidos += 1
    return {"invalidos": invalidos, "preservados": preservados}


_CEP = re.compile(r"\b(\d{5})-?(\d{3})\b")
_CIDADE_END = re.compile(r"([^,\-]+?)\s*-\s*([A-Z]{2})\b")


def aproveitar_campos_salvos(empresa, pesquisa_id=None):
    """Preenche vazio com o que o resultado desta empresa já traz. Não copia cidade da consulta nem razão inventada."""
    campos = []
    if not (empresa.cep or "").strip():
        m = _CEP.search(empresa.endereco or "")
        if not m:
            for cw in _consultas_visiveis(empresa, pesquisa_id).iterator():
                for item in cw.resultados or []:
                    if item_refere_empresa(empresa, item):
                        m = _CEP.search(item.get("endereco_citado") or "")
                        if m:
                            break
                if m:
                    break
        if m:
            empresa.cep = m.group(1) + m.group(2)
            campos.append("cep")
            registrar_dado(empresa, "cep_no_endereco", empresa.cep, "resultado_salvo", "endereço já gravado")
    if not empresa.dados_coletados.filter(campo="atividade_publicada").exists():
        achou_atividade = False
        for cw in _consultas_visiveis(empresa, pesquisa_id).iterator():
            for item in cw.resultados or []:
                if not item_refere_empresa(empresa, item):
                    continue
                atv = (item.get("atividade") or "").strip()
                if not atv:
                    continue
                registrar_dado(empresa, "atividade_publicada", atv[:200], "resultado_salvo", f"consulta {cw.pk}")
                achou_atividade = True
                break
            if achou_atividade:
                break
    if not (empresa.cidade or "").strip():
        for cw in _consultas_visiveis(empresa, pesquisa_id).iterator():
            for item in cw.resultados or []:
                if not item_refere_empresa(empresa, item):
                    continue
                achados = _CIDADE_END.findall(item.get("endereco_citado") or "")
                if not achados:
                    continue
                empresa.cidade = achados[-1][0].strip()[:80]
                empresa.uf = achados[-1][1]
                campos += ["cidade", "uf"]
                break
            if "cidade" in campos:
                break
    if campos:
        empresa.save(update_fields=list(dict.fromkeys(campos + ["atualizado_em"])))
    return campos


def completar_identificacao(empresa, sel, ctx=None, busca=None, orc=None, max_buscas=0, feitas=None,
                            permitir_consulta_complementar=True):
    """Completa a identificação de uma empresa. Não gasta busca se max_buscas=0. Não escreve no Salesforce."""
    from prospeccao.models import SelecaoPesquisa
    from prospeccao.services import oportunidades as ops
    from prospeccao.services.descoberta_google import adotar_cnpj, norm_consulta
    from prospeccao.services.orcamento import buscar as buscar_pago

    feitas = list(feitas or [])
    info = {"buscas_novas": 0, "consultas_novas": [], "motivo": "", "terminal": False, "acao": ""}
    pesquisa_id = sel.pesquisa_id if sel is not None else None
    consolidar_sites(empresa)
    aproveitar_campos_salvos(empresa, pesquisa_id)
    conhecida = sel is not None and sel.fila == SelecaoPesquisa.Fila.CONHECIDA
    info["revisao"] = revisar_associacoes(empresa, pesquisa_id, preservar_conhecida=conhecida)
    recuperar_contatos_salvos(empresa, pesquisa_id)
    if empresa.cnpj and empresa.situacao_cadastral and empresa.situacao_cadastral_referencia:
        from datetime import date, timedelta
        from carteira.models import ConfiguracaoAnalise
        dias = int(ConfiguracaoAnalise.obter().dias_validade_situacao_cadastral)
        if empresa.situacao_cadastral_referencia >= date.today() - timedelta(days=dias):
            info.update(motivo="CNPJ já identificado e situação cadastral já registrada", terminal=True, acao="ja_resolvida")
            return info
    adotado = aproveitar_cnpj_publicado(empresa, sel, consultar=permitir_consulta_complementar)
    if adotado:
        return adotado
    pesquisa_id = sel.pesquisa_id if sel is not None else None
    cidade = empresa.cidade or ""
    if not cidade and sel is not None:
        from prospeccao.models import linhas
        cidade = (linhas(sel.pesquisa.cidades) or [""])[0]
    candidatos = []
    for texto, url, fonte in _textos(empresa, pesquisa_id):
        validos, _invalidos = cnpjs_no_texto(texto)
        for cnpj in validos:
            candidatos.append((cnpj, fonte, url))
    for valor in empresa.dados_coletados.filter(campo="cnpj_candidato").values_list("valor", flat=True):
        for cnpj in cnpjs_no_texto(str(valor))[0]:
            candidatos.append((cnpj, "cnpj_candidato", ""))
    vistos = set()
    ambiguo = False
    pendente = None  # CNPJ citado que não deu para consultar: só vira resposta se nada mais resolver
    for cnpj, fonte, url in candidatos:
        if cnpj in vistos or (empresa.cnpj and empresa.cnpj != cnpj):
            continue
        if empresa.dados_coletados.filter(campo="cnpj_candidato_rejeitado", valor=cnpj,
                                          referencia__startswith=VERSAO_REGRAS).exists():
            continue  # rejeições das regras anteriores são reavaliadas uma vez pelas regras atuais
        vistos.add(cnpj)
        from prospeccao.conectores.receita import consultar_local
        dados, carga = consultar_local(cnpj)
        if dados is None and permitir_consulta_complementar:
            status, extra = _consultar_cadastral(cnpj, empresa)
            if status == "ok":
                dados = extra
            elif status == "sem_retorno":
                registrar_dado(empresa, "cnpj_candidato", cnpj, fonte[:60],
                               "consulta cadastral não encontrou este CNPJ; não é falha de provedor")
                continue
            else:
                registrar_dado(empresa, "consulta_cadastral_pendente", f"{cnpj}: {status} {extra or ''}"[:300],
                               "brasilapi_cnpj", url)
                pendente = pendente or (cnpj, url, (f"CNPJ {cnpj} visto na fonte. A consulta cadastral não concluiu "
                                                    f"({status}). Não é ausência de cadastro."))
                continue  # os outros candidatos e o cruzamento pela base ainda podem resolver
        elif dados is None:
            registrar_dado(empresa, "consulta_cadastral_pendente", f"{cnpj}: base local sem este CNPJ",
                           "receita_dados_abertos", url)
            pendente = pendente or (cnpj, url, (f"CNPJ {cnpj} visto na fonte. Base local da Receita não tem este "
                                                "cadastro e a consulta externa não foi autorizada nesta etapa."))
            continue
        r = resolver_candidato(empresa, sel, cnpj, dados, carga, ctx, f"fonte {fonte}",
                               permitir_consulta_complementar)
        if r is not None and r.get("acao") == "ambiguo":
            ambiguo = r["motivo"]
            continue
        if r is not None:
            info.update({k: v for k, v in r.items() if k not in ("buscas_novas", "consultas_novas")})
            return info
    if empresa.cnpj:
        from datetime import date, timedelta
        from carteira.models import ConfiguracaoAnalise
        dias = int(ConfiguracaoAnalise.obter().dias_validade_situacao_cadastral)
        ref = empresa.situacao_cadastral_referencia
        fresca = bool(empresa.situacao_cadastral and ref and ref >= date.today() - timedelta(days=dias))
        if not fresca and permitir_consulta_complementar:
            _aplicar_situacao(empresa, None, None, ctx)
            empresa.refresh_from_db()
        _recruzar(empresa, sel)
        if not empresa.situacao_cadastral:
            motivo = "CNPJ associado. A situação cadastral continua pendente."
        else:
            motivo = "CNPJ já estava identificado; situação e cruzamento revistos"
        info.update(motivo=motivo, terminal=True, acao="revisto")
        registrar_dado(empresa, "motivo_encerramento_identificacao", info["motivo"], "identificacao", "")
        return info
    # base pública da Receita antes de gastar busca no Google
    pela_base = identificar_pela_base(empresa, sel, ctx, permitir_consulta_complementar)
    if pela_base is not None and pela_base.get("acao") != "ambiguo":
        info.update({k: v for k, v in pela_base.items() if k not in ("buscas_novas", "consultas_novas")})
        return info
    if pela_base is not None and not ambiguo:
        ambiguo = pela_base["motivo"]
    if pendente and not ambiguo and (max_buscas <= 0 or busca is None):
        # nada resolveu e não há busca dirigida nesta etapa: o CNPJ citado sem consulta é a pendência real
        info.update(motivo=pendente[2], terminal=True, acao="cadastral_pendente")
        registrar_dado(empresa, "motivo_encerramento_identificacao", info["motivo"], "identificacao", pendente[1])
        return info
    if ambiguo:
        extra = empresa.dados_coletados.filter(campo="proxima_acao").values_list("valor", flat=True).first()
        motivo = ambiguo if isinstance(ambiguo, str) else "Identidade a conferir: candidato encontrado sem vínculo seguro"
        if extra:
            motivo = f"{extra} {motivo}"
        info.update(motivo=motivo, terminal=True, acao="ambiguo")
        registrar_dado(empresa, "motivo_encerramento_identificacao", info["motivo"], "identificacao", "")
        return info
    consulta = f'"{empresa.nome}" "{cidade}" CNPJ'.strip()
    ja = {norm_consulta(q) for q in feitas}
    ja_qs = ConsultaWeb.objects.filter(empresa=empresa, finalidade="COMPLEMENTAR", status="OK")
    if busca is not None and getattr(busca, "codigo", ""):
        ja_qs = ja_qs.filter(provedor=busca.codigo)  # a mesma consulta em outro provedor não é repetição
    ja |= {norm_consulta(q) for q in ja_qs.values_list("consulta", flat=True)}
    # Busca dirigida feita pelo PROGRAMA (não depende de a IA escolher BUSCAR_GOOGLE nem de haver chamada de IA):
    # nome + município primeiro; depois telefone e site já salvos, até o limite de buscas da empresa.
    from prospeccao.services.lote_identificacao import montar_consultas
    roteiro = []
    variacoes = [q for q in montar_consultas(empresa) if not q.startswith(f'"{empresa.nome}"')]  # nome já coberto
    for q in [consulta] + variacoes:
        if q and norm_consulta(q) not in ja and norm_consulta(q) not in {norm_consulta(x) for x in roteiro}:
            roteiro.append(q)
    if roteiro:
        consulta = roteiro[0]
    if max_buscas <= 0 or busca is None or orc is None or not roteiro:
        if max_buscas <= 0 and ja:
            porque = f"{len(ja)} busca(s) dirigida(s) já feita(s) sem CNPJ compatível"
        elif max_buscas <= 0:
            porque = "busca dirigida ainda não feita; aguarda o lote de identificação"
        elif not roteiro:
            porque = "consulta dirigida já feita"
        else:
            porque = "busca indisponível"
        pendente = empresa.dados_coletados.filter(campo="proxima_acao").values_list("valor", flat=True).first()
        if pendente:
            porque = pendente
        info.update(motivo=porque if pendente else f"CNPJ não identificado nas fontes já salvas. {porque}.",
                    terminal=True, acao="consulta_indisponivel" if pendente else "pendente_sem_busca")
        registrar_dado(empresa, "motivo_encerramento_identificacao", info["motivo"], "identificacao", "")
        return info
    if getattr(orc, "busca_bloqueada", None):
        info.update(motivo="CNPJ não identificado nas fontes já salvas. Busca bloqueada pelo orçamento.",
                    terminal=True, acao="limite")
        registrar_dado(empresa, "motivo_encerramento_identificacao", info["motivo"], "identificacao", "")
        return info
    for consulta in roteiro[:max(1, int(max_buscas))]:
        if getattr(orc, "busca_bloqueada", None):
            break
        fim = _busca_dirigida(empresa, sel, ctx, busca, orc, consulta, info, buscar_pago, adotar_cnpj, ops,
                              permitir_consulta_complementar)
        if fim is not None:
            return fim
    if getattr(orc, "busca_bloqueada", None) and not info["buscas_novas"]:
        info.update(motivo="CNPJ não identificado nas fontes já salvas. Busca bloqueada pelo orçamento.",
                    terminal=True, acao="limite")
        registrar_dado(empresa, "motivo_encerramento_identificacao", info["motivo"], "identificacao", "")
        return info
    n = info["buscas_novas"]
    info.update(motivo=f"CNPJ não identificado após as fontes salvas e {n} busca(s) dirigida(s).",
                terminal=True, acao="pendente_busca")
    registrar_dado(empresa, "motivo_encerramento_identificacao", info["motivo"], "identificacao", "")
    return info


def _busca_dirigida(empresa, sel, ctx, busca, orc, consulta, info, buscar_pago, adotar_cnpj, ops,
                    permitir_consulta_complementar=True):
    """Uma busca dirigida de CNPJ. Retorna info terminal quando conclui; None para tentar a próxima variação."""
    contexto = {"modalidade": "organico"}
    if getattr(busca, "lote", False):  # OpenAI web_search: pedido por EMPRESA, focado no CNPJ e nas unidades
        contexto = {"modalidade": "organico", "objetivo": "cnpj", "cidade": empresa.cidade or "", "uf": empresa.uf or "SP",
                    "empresa": {"nome": empresa.nome, "municipio": empresa.cidade or "", "uf": empresa.uf or "SP",
                                "endereco": (empresa.endereco or "")[:150],
                                "telefones": list(empresa.canais.filter(tipo="TELEFONE")
                                                  .values_list("valor", flat=True)[:3])}}
    res = buscar_pago(
        busca, consulta, finalidade="COMPLEMENTAR", orc=orc, ctx=ctx, empresa=empresa,
        contexto=contexto,
        motivo="identificação cadastral: nome comercial, município da pesquisa e CNPJ")
    info["buscas_novas"] += 1
    info["consultas_novas"].append(consulta)
    reg = getattr(orc, "ultimo_registro", None)
    if reg is not None and reg.status == "FALHA":
        info.update(motivo=f"Falha técnica na busca dirigida ({(reg.erro or '')[:160]}). Não é ausência de CNPJ.",
                    terminal=True, acao="falha_provedor")
        registrar_dado(empresa, "motivo_encerramento_identificacao", info["motivo"], "identificacao", "")
        return info
    for item in res or []:
        if not isinstance(item, dict):
            continue
        blob = " ".join(str(item.get(k) or "") for k in ("titulo", "trecho", "nome_citado", "cnpj_citado"))
        for cnpj in cnpjs_no_texto(blob)[0]:
            from prospeccao.conectores.receita import consultar_local
            dados, carga = consultar_local(cnpj)
            if dados is None and permitir_consulta_complementar:
                status, extra = _consultar_cadastral(cnpj, empresa)  # CNPJ fora do recorte local: consulta pública
                dados = extra if status == "ok" else None
            r = resolver_candidato(empresa, sel, cnpj, dados, carga, ctx, "busca dirigida",
                                   permitir_consulta_complementar)
            if r is not None:
                info.update({k: v for k, v in r.items() if k not in ("buscas_novas", "consultas_novas")})
                if r.get("acao") == "ambiguo":
                    info["motivo"] = "Identidade a conferir após busca dirigida: " + r["motivo"].split(": ", 1)[-1]
                registrar_dado(empresa, "motivo_encerramento_identificacao", info["motivo"], "identificacao", "")
                return info
    return None
