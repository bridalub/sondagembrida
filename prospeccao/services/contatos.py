"""
Descoberta e qualificação de canais de contato (telefone, e-mail, WhatsApp explícito).

Dimensões SEPARADAS em cada canal:
  origem (cadastral x publicação externa x equipe) · confiança na ASSOCIAÇÃO com a empresa ·
  verificações TÉCNICAS (formato, DDD, domínio/MX) · confirmação HUMANA (atendeu / incorreto).

Regras:
  - nunca inventar DDD, número, pessoa ou endereço ("contato@dominio" NÃO é gerado);
  - celular não implica WhatsApp: só link wa.me/api.whatsapp publicado conta como WhatsApp;
  - telefone/e-mail usado por várias empresas de grupos diferentes = compartilhado (provável contador/terceiro);
  - mesmo telefone em vários estabelecimentos do mesmo grupo = central do grupo;
  - domínio válido (MX) não prova que a caixa existe;
  - finalidade só pelo que está escrito (prefixo do e-mail ou rótulo da página); sem sinal = desconhecida;
  - associação do site por evidências COMBINADAS (CNPJ, raiz = grupo, nome + telefone/CEP/endereço/município);
    sites prováveis e conflitos ficam para revisão; diretórios são pistas e não confirmam identidade.
"""
import re
import socket
from functools import lru_cache

from django.utils import timezone

from carteira.models import CanalEmpresa
from carteira.services.normalizacao import DOMINIOS_GENERICOS, classificar_email, digitos_telefone, dominio_de_url

DDDS_VALIDOS = {11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 24, 27, 28, 31, 32, 33, 34, 35, 37, 38, 41, 42, 43, 44, 45,
                46, 47, 48, 49, 51, 53, 54, 55, 61, 62, 63, 64, 65, 66, 67, 68, 69, 71, 73, 74, 75, 77, 79, 81, 82, 83,
                84, 85, 86, 87, 88, 89, 91, 92, 93, 94, 95, 96, 97, 98, 99}
_CONTADOR = re.compile(r"contab|contador|escritorio|assessoriacont|tributar", re.I)
ROTULO_FINALIDADE = {"compras": "COMPRAS", "suprimentos": "COMPRAS", "manutenção": "MANUTENCAO", "manutencao": "MANUTENCAO",
                     "frota": "MANUTENCAO", "oficina": "MANUTENCAO", "comercial": "COMERCIAL", "vendas": "COMERCIAL",
                     "atendimento": "ATENDIMENTO", "sac": "ATENDIMENTO", "ouvidoria": "ATENDIMENTO",
                     "financeiro": "FINANCEIRO", "cobrança": "FINANCEIRO", "cobranca": "FINANCEIRO", "fiscal": "FISCAL"}


@lru_cache(maxsize=2048)
def dominio_tem_mx(dominio):
    """True/False/None (None = não foi possível verificar). Não prova que a caixa existe."""
    try:
        import dns.resolver
        try:
            return bool(dns.resolver.resolve(dominio, "MX", lifetime=5))
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            return False
    except Exception:
        return None


@lru_cache(maxsize=2048)
def dominio_resolve(dominio):
    try:
        socket.getaddrinfo(dominio, 443)
        return True
    except OSError:
        return False


_MASCARA = re.compile(r"[*•…]|x{3,}|\[email|protected", re.I)


def mascarado(valor):
    """E-mail/telefone ocultado pela fonte (ex.: sa****@****.com, (11) 4709-****). Nunca é reconstruído."""
    return bool(_MASCARA.search(valor or ""))


def verificar_telefone(valor):
    if mascarado(valor):
        return "", {"formato": "mascarado"}
    d = digitos_telefone(valor)
    if not d:
        return "", {"formato": "invalido_ou_sem_ddd"}
    tipo = "celular" if len(d) == 11 and d[2] == "9" else "fixo"
    return d, {"formato": "ok", "ddd_valido": int(d[:2]) in DDDS_VALIDOS, "tipo": tipo,
               "whatsapp": "não presumido (só link publicado)"}


def verificar_email(valor):
    v = (valor or "").strip().lower()
    if mascarado(v):
        return "", {"formato": "mascarado"}
    if not re.fullmatch(r"[^@\s;,]+@[^@\s;,]+\.[a-z]{2,}", v):
        return "", {"formato": "invalido"}
    dom = v.split("@")[1]
    return v, {"formato": "ok", "dominio": dom, "dominio_generico": dom in DOMINIOS_GENERICOS,
               "mx": dominio_tem_mx(dom), "observacao": "MX válido não comprova que a caixa existe"}


def compartilhamento(empresa, tipo, normalizado):
    """('', None) | ('texto', aplica_a) a partir da base Receita (quantas empresas usam o mesmo canal)."""
    from receita.models import CargaReceita, EstabelecimentoReceita
    carga = CargaReceita.ativa_atual()
    if not carga or not normalizado:
        return "", None
    if tipo == CanalEmpresa.Tipo.TELEFONE:
        qs = EstabelecimentoReceita.objects.filter(carga=carga, telefone1=normalizado) | \
            EstabelecimentoReceita.objects.filter(carga=carga, telefone2=normalizado)
    elif tipo == CanalEmpresa.Tipo.EMAIL:
        qs = EstabelecimentoReceita.objects.filter(carga=carga, email=normalizado)
    else:
        return "", None
    raizes = set(qs.values_list("cnpj_basico", flat=True)[:200])
    outras = raizes - {empresa.cnpj[:8] if empresa.cnpj else ""}
    if len(outras) >= 3:
        return f"usado por {len(raizes)} empresas de grupos diferentes na base Receita (provável contador/terceiro)", \
            CanalEmpresa.AplicaA.CONTADOR
    mesmos = qs.filter(cnpj_basico=empresa.cnpj[:8]).count() if empresa.cnpj else 0
    if mesmos > 1:
        return f"usado por {mesmos} estabelecimentos do mesmo grupo (central do grupo)", CanalEmpresa.AplicaA.GRUPO
    return "", None


def registrar(empresa, tipo, valor, *, fonte, url="", origem, confianca, aplica_a, evidencia="", departamento="",
              whatsapp=False, finalidade=None):
    """Cria/atualiza um canal; nunca rebaixa confirmação humana nem confiança já mais alta."""
    if mascarado(valor):  # não vira canal; o original fica no histórico da fonte
        from carteira.models import DadoColetado
        campo = ("email" if tipo == CanalEmpresa.Tipo.EMAIL else "telefone") + f"_nao_disponibilizado:{fonte}"
        DadoColetado.objects.update_or_create(
            empresa=empresa, campo=campo[:60], fonte=fonte[:60],
            defaults={"valor": f"{valor} (ocultado pela fonte – não utilizável, não reconstruído)",
                      "referencia": (url or "")[:500], "coletado_em": timezone.now()})
        return None
    if tipo in (CanalEmpresa.Tipo.TELEFONE, "WHATSAPP"):
        normal, tec = verificar_telefone(valor)
        tipo = CanalEmpresa.Tipo.TELEFONE
    elif tipo == CanalEmpresa.Tipo.EMAIL:
        normal, tec = verificar_email(valor)
    else:
        normal, tec = (valor or "").strip(), {}
    if not normal:
        return None
    if tipo == CanalEmpresa.Tipo.EMAIL:
        finalidade = finalidade or classificar_email(normal)
        if _CONTADOR.search(normal.split("@")[1]):
            finalidade, aplica_a, confianca = "CONTADOR", CanalEmpresa.AplicaA.CONTADOR, CanalEmpresa.Confianca.CONFLITO
            evidencia = (evidencia + "; " if evidencia else "") + "domínio de escritório contábil"
    comp, aplica_comp = compartilhamento(empresa, tipo, normal)
    if not comp and origem == CanalEmpresa.Origem.PUBLICACAO:
        qs = (CanalEmpresa.objects.filter(tipo=tipo, valor_normalizado=normal, origem_canal=CanalEmpresa.Origem.PUBLICACAO)
              .exclude(empresa=empresa).select_related("empresa"))
        doc_atual = (getattr(empresa, "cnpj", None) or "").strip()
        if doc_atual:
            qs = qs.exclude(empresa__cnpj__startswith=doc_atual[:8])
        rotulos, ids = [], []
        for outro in qs[:8]:
            emp = getattr(outro, "empresa", None)
            if emp is None:
                continue
            nome = (getattr(emp, "nome", None) or "").strip()
            doc = (getattr(emp, "cnpj", None) or "").strip()
            if doc_atual and doc and doc[:8] == doc_atual[:8]:
                continue
            rotulo = nome or doc or f"cadastro {emp.pk}"
            if not rotulo or rotulo == "None":
                continue
            rotulos.append(rotulo)
            if outro.confirmacao_humana not in (
                    CanalEmpresa.Confirmacao.ATENDIDO, CanalEmpresa.Confirmacao.CONFERIDO):
                ids.append(outro.pk)
        if rotulos:
            comp = ("também publicado para outra(s) empresa(s) (" + ", ".join(rotulos)
                    + ") – possível relação, não é fusão")
            confianca, aplica_comp = CanalEmpresa.Confianca.CONFLITO, CanalEmpresa.AplicaA.DESCONHECIDO
            if ids:
                quem = (getattr(empresa, "nome", None) or "").strip() or doc_atual or f"cadastro {empresa.pk}"
                CanalEmpresa.objects.filter(pk__in=ids).update(
                    confianca_associacao=CanalEmpresa.Confianca.CONFLITO,
                    compartilhado=(f"também publicado para {quem} (outro cadastro) – possível relação, não é fusão")[:200])
    if aplica_comp == CanalEmpresa.AplicaA.CONTADOR:
        aplica_a, confianca = aplica_comp, CanalEmpresa.Confianca.CONFLITO
        finalidade = "CONTADOR" if tipo == CanalEmpresa.Tipo.EMAIL else finalidade
    elif aplica_comp == CanalEmpresa.AplicaA.GRUPO and aplica_a == CanalEmpresa.AplicaA.ESTABELECIMENTO:
        aplica_a = aplica_comp
    agora = timezone.now()
    legado = {CanalEmpresa.Confianca.ALTA: "CONFIRMADO_FONTE", CanalEmpresa.Confianca.CONFLITO: "CONFLITO"}.get(
        confianca, "CADASTRAL" if origem == CanalEmpresa.Origem.CADASTRAL else "ENCONTRADO")
    obj, criado = CanalEmpresa.objects.get_or_create(
        empresa=empresa, tipo=tipo, valor=normal[:300],
        defaults=dict(valor_normalizado=normal[:300], fonte=fonte, referencia=url[:500], origem_canal=origem,
                      confianca_associacao=confianca, aplica_a=aplica_a, evidencia_associacao=evidencia[:500],
                      departamento=departamento[:120], compartilhado=comp[:200], whatsapp_explicito=whatsapp,
                      verificacao_tecnica=tec, conferido_em=agora, classificacao=finalidade or "INCERTO",
                      verificacao=legado, primeira_coleta_em=agora, coletado_em=agora))
    if not criado:
        ordem = ["CONFLITO", "BAIXA", "MEDIA", "ALTA"]
        campos = ["coletado_em", "conferido_em", "verificacao_tecnica", "compartilhado"]
        dom_novo = dominio_de_url(url) or fonte
        dom_orig = dominio_de_url(obj.referencia) or obj.fonte
        doms = {c.get("dominio") for c in obj.corroboracoes or []}
        if dom_novo and dom_novo != dom_orig and dom_novo not in doms:  # outra fonte (domínio diferente)
            obj.corroboracoes = (obj.corroboracoes or []) + [
                {"fonte": fonte[:60], "url": url[:500], "dominio": dom_novo[:120], "origem": origem,
                 "confianca": confianca, "evidencia": evidencia[:200], "em": agora.isoformat()}]
            campos.append("corroboracoes")
        obj.coletado_em = obj.conferido_em = agora
        obj.verificacao_tecnica, obj.compartilhado = tec, comp[:200]
        if obj.confirmacao_humana == CanalEmpresa.Confirmacao.INCORRETO:
            obj.save(update_fields=campos)
            return obj
        if obj.confirmacao_humana != CanalEmpresa.Confirmacao.ATENDIDO:
            if confianca == CanalEmpresa.Confianca.CONFLITO or ordem.index(confianca) > ordem.index(obj.confianca_associacao):
                obj.confianca_associacao, obj.aplica_a = confianca, aplica_a
                campos += ["confianca_associacao", "aplica_a"]
            elif aplica_comp and obj.aplica_a != aplica_a:  # compartilhamento detectado depois (grupo/contador)
                obj.aplica_a = aplica_a
                campos.append("aplica_a")
            if finalidade and obj.classificacao in ("INCERTO", "") and finalidade != obj.classificacao:
                obj.classificacao = finalidade
                campos.append("classificacao")
        if whatsapp and not obj.whatsapp_explicito:
            obj.whatsapp_explicito = True
            campos.append("whatsapp_explicito")
        if departamento and not obj.departamento:
            obj.departamento = departamento[:120]
            campos.append("departamento")
        if not obj.valor_normalizado:
            obj.valor_normalizado = normal[:300]
            campos.append("valor_normalizado")
        obj.save(update_fields=campos)
    return obj


def qualificar_cadastrais(empresa):
    """Recalcula compartilhamento/contador/técnica dos canais cadastrais (Receita)."""
    for c in empresa.canais.filter(origem_canal=CanalEmpresa.Origem.CADASTRAL):
        registrar(empresa, c.tipo, c.valor, fonte=c.fonte, url=c.referencia, origem=c.origem_canal,
                  confianca=CanalEmpresa.Confianca.MEDIA, aplica_a=CanalEmpresa.AplicaA.ESTABELECIMENTO,
                  evidencia="cadastro do CNPJ na Receita (pode ser do contador; não validado)")


# ------------------------------------------------------------------ descoberta de site

def consultas_empresa(empresa, consultas, max_buscas):
    qs = []
    for modelo in list(consultas):
        try:
            q = modelo.format(cnpj_formatado=empresa.cnpj_formatado, razao_social=empresa.razao_social or empresa.nome,
                              nome_fantasia=empresa.nome_fantasia or empresa.razao_social or "",
                              municipio=empresa.cidade or "")
        except (KeyError, IndexError):
            continue
        q = " ".join(q.split())
        if q.strip('" ') and q not in qs:
            qs.append(q)
    return qs[:max_buscas]


def candidatos_gratuitos(empresa):
    """Site já conhecido, sites já encontrados na pesquisa (inclusive de outra unidade da MESMA RAIZ de CNPJ) e
    domínio próprio do e-mail (sem custo). A associação depois diz se é da unidade, da matriz ou do grupo."""
    from prospeccao.models import Candidato
    from prospeccao.services.pipeline import _dominio_suspeito
    urls = []
    if empresa.site:
        urls.append((empresa.site, "site já conhecido"))
    for c in empresa.canais.filter(tipo=CanalEmpresa.Tipo.SITE).exclude(confianca_associacao="CONFLITO"):
        urls.append((c.valor, f"site indicado por {c.fonte}"))
    vistos_cand = Candidato.objects.filter(empresa=empresa).exclude(site="")
    if empresa.cnpj:
        vistos_cand = vistos_cand | Candidato.objects.filter(cnpj_candidato__startswith=empresa.cnpj[:8]).exclude(site="")
    for c in vistos_cand.distinct()[:5]:
        mesma = c.cnpj_candidato and empresa.cnpj and c.cnpj_candidato != empresa.cnpj
        urls.append((c.site, "site encontrado na pesquisa" + (f" (CNPJ {c.cnpj_candidato}, mesma raiz)" if mesma else "")))
    for c in empresa.canais.filter(tipo=CanalEmpresa.Tipo.EMAIL):
        dom = c.valor.split("@")[-1]
        if dom not in DOMINIOS_GENERICOS and not _dominio_suspeito(dom):
            urls.append((f"https://{dom}", f"domínio do e-mail {c.get_origem_canal_display().lower()}"))
    return urls


def candidatos_busca(empresa, buscar_fn, consultas, max_buscas, pistas=None, meta=None, motivo=""):
    """Sites vindos da pesquisa web (paga). Diretórios/redes sociais vão só para `pistas`.
    `meta` (dict) recebe a análise do retorno feita pela IA (faltando / próximas consultas)."""
    import inspect
    urls = []
    qs = consultas_empresa(empresa, consultas, max_buscas) if buscar_fn is not None else []
    if qs:
        aceita_motivo = "motivo" in inspect.signature(buscar_fn).parameters
        res = buscar_fn(qs, empresa, motivo=motivo) if aceita_motivo else buscar_fn(qs, empresa)
        res = [] if res is None else res  # lista vazia ainda traz a análise da IA (o que faltou)
        if meta is not None:
            meta["analise"] = getattr(res, "analise", None) or {}
            meta["consultas"] = qs
        for r in res:
            como = f"busca web ({r.get('verificacao', 'resultado')}): {' | '.join(qs)[:150]}"
            if pistas is not None:
                pistas.append({**r, "como": como})
            if not r.get("diretorio"):
                urls.append((r["url"], como))
    return urls


def _unicos(urls, vistos=None):
    vistos = set() if vistos is None else vistos
    saida = []
    for u, como in urls:
        d = dominio_de_url(u)
        if d and d not in vistos:
            vistos.add(d)
            saida.append((u, como))
    return saida


def candidatos_site(empresa, buscar_fn=None, consultas=(), max_buscas=3, pistas=None):
    """
    Lista de (url, como_foi_encontrado). NÃO adivinha domínios. Fontes:
      1. site já conhecido; 2. domínio próprio do e-mail (sem contador/compartilhado);
      3. busca web por CNPJ / razão social / nome fantasia + município (também para quem não tem domínio próprio
         ou usa Gmail/Outlook), limitada a `max_buscas` consultas por empresa.
    Resultados de diretório/rede social NÃO viram site candidato: vão para `pistas` (lista mutável, se fornecida)
    junto com os dados que a busca diz ter visto, para registro como pista a revisar.
    Os sites ainda passam pela conferência de associação (evidências combinadas).
    """
    from prospeccao.services.pipeline import _dominio_suspeito
    urls = []
    if empresa.site:
        urls.append((empresa.site, "site já conhecido"))
    for c in empresa.canais.filter(tipo=CanalEmpresa.Tipo.EMAIL):
        dom = c.valor.split("@")[-1]
        if dom not in DOMINIOS_GENERICOS and not _dominio_suspeito(dom):
            urls.append((f"https://{dom}", f"domínio do e-mail {c.get_origem_canal_display().lower()}"))
    qs = consultas_empresa(empresa, consultas, max_buscas) if buscar_fn is not None else []
    if qs:
        for r in buscar_fn(qs, empresa) or []:
            como = f"busca web ({r.get('verificacao', 'resultado')}): {' | '.join(qs)[:150]}"
            if pistas is not None:
                pistas.append({**r, "como": como})
            if not r.get("diretorio"):
                urls.append((r["url"], como))
    vistos, saida = set(), []
    for u, como in urls:
        d = dominio_de_url(u)
        if d and d not in vistos:
            vistos.add(d)
            saida.append((u, como))
    return saida


def _digitos(t):
    return re.sub(r"\D", "", t or "")


def sinais_identidade(empresa, paginas):
    """Evidências combinadas no texto do site: nome distintivo, CEP, logradouro, município e telefone cadastral."""
    from carteira.services.normalizacao import normalizar_texto
    from prospeccao.services.pipeline import _site_menciona_empresa
    texto = normalizar_texto(" ".join(t[:60000] for _, t in paginas))
    dig = _digitos(texto)
    url = paginas[0][0] if paginas else ""
    s = {"nome": _site_menciona_empresa(empresa, dominio_de_url(url), paginas)}
    cep = _digitos(empresa.cep)
    s["cep"] = len(cep) == 8 and cep in dig
    tels = {_digitos(c.valor) for c in empresa.canais.filter(tipo=CanalEmpresa.Tipo.TELEFONE,
                                                             origem_canal=CanalEmpresa.Origem.CADASTRAL)}
    tels |= {_digitos(empresa.telefone)} if empresa.telefone else set()
    s["telefone"] = any(len(t) >= 10 and t[2:] in dig for t in tels if t)
    cidade = normalizar_texto(empresa.cidade or "")
    s["municipio"] = bool(cidade) and f" {cidade} " in f" {texto} "
    rua = [w for w in normalizar_texto((empresa.endereco or "").split("–")[0]).split()
           if len(w) >= 4 and w not in {"RUA", "AVENIDA", "ESTRADA", "RODOVIA", "TRAVESSA", "ALAMEDA"}]
    s["logradouro"] = len(rua) >= 1 and all(f" {w} " in f" {texto} " for w in rua[:3])
    return s


def nomes_citados_com_cnpj(empresa, pistas):
    """Nomes que fontes publicam JUNTO com o CNPJ desta empresa (ex.: nome fantasia em diretório). Servem de
    evidência adicional, nunca sozinhos."""
    nomes = []
    for p in pistas or []:
        if _digitos(p.get("cnpj_citado")) == empresa.cnpj:
            for n in re.split(r"[;—–|]| - ", p.get("nome_citado") or ""):
                n = n.strip()
                if len(n) >= 4 and n not in nomes:
                    nomes.append(n)
    return nomes


def _nome_alt_no_site(nomes, paginas, dominio):
    from carteira.services.normalizacao import normalizar_texto
    from prospeccao.services.identificacao import tokens_distintivos
    texto = f" {normalizar_texto(' '.join(t[:60000] for _, t in paginas))} "
    dom = normalizar_texto((dominio or "").replace(".", " ")).replace(" ", "")
    for n in nomes:
        toks = [t for t in tokens_distintivos(n) if len(t) >= 4]
        if toks and all(f" {t} " in texto or t in dom for t in toks[:3]):
            return n
    return ""


def associar_site(empresa, res, como, nomes_alt=()):
    """
    (confianca, aplica_a, evidencia, usar_contatos) ou None se o site não for da empresa. Regras:
      - mesmo CNPJ publicado -> ALTA (estabelecimento; empresa se o site lista vários CNPJs);
      - só outro CNPJ da mesma raiz -> MÉDIA, canais do GRUPO (possível vínculo; não confirma a filial);
      - CNPJ de outro grupo publicado + nome da empresa -> CONFLITO (mantido para revisão, contatos não usados);
      - sem CNPJ: nome distintivo + (telefone cadastral ou CEP) -> MÉDIA; nome + endereço/município, ou origem
        confiável (domínio do e-mail / site conhecido) + nome -> MÉDIA; só o nome -> BAIXA (site provável, revisão);
      - sem nome -> não associado.
    """
    publicado = set(res.cnpjs)
    if not empresa.cnpj:
        # empresa achada sem CNPJ: só o CNPJ do trecho desta empresa vira evidência
        if publicado:
            from carteira.models import DadoColetado
            texto = "\n".join(t for _, t in res.paginas)
            proprios = []
            if pagina_lista_varias_empresas(texto, publicado, dominio_de_url(res.paginas[0][0] if res.paginas else "")):
                from prospeccao.services.identificacao_cnpj import tokens_que_identificam
                toks = tokens_que_identificam(empresa)
                for cnpj in sorted(publicado):
                    pos = texto.find(cnpj)
                    if pos < 0:
                        fmt = f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"
                        pos = texto.find(fmt)
                    trecho = normalizar_pagina(texto[max(0, pos - 180):pos + 20]) if pos >= 0 else ""
                    if toks and any(t in trecho.split() for t in toks):
                        proprios.append(cnpj)
            else:
                proprios = sorted(publicado)
            if proprios:
                DadoColetado.objects.update_or_create(
                    empresa=empresa, campo="cnpj_publicado_no_site", fonte="site_publico",
                    defaults={"valor": ", ".join(proprios)[:200],
                              "referencia": (res.paginas[0][0] if res.paginas else "")[:500]})
        cnpjs = set()
    else:
        cnpjs = publicado
    if empresa.cnpj and empresa.cnpj in cnpjs:
        return CanalEmpresa.Confianca.ALTA, CanalEmpresa.AplicaA.ESTABELECIMENTO if len(cnpjs) == 1 else \
            CanalEmpresa.AplicaA.EMPRESA, f"site publica o CNPJ {empresa.cnpj_formatado} ({como})", True
    mesma_raiz = sorted(c for c in cnpjs if empresa.cnpj and c[:8] == empresa.cnpj[:8])
    if mesma_raiz:
        if empresa.cnpj[8:12] != "0001" and all(c[8:12] == "0001" for c in mesma_raiz):
            return CanalEmpresa.Confianca.MEDIA, CanalEmpresa.AplicaA.MATRIZ, \
                (f"site publica só o CNPJ da MATRIZ ({mesma_raiz[0]}); este CNPJ é filial – contatos tratados como "
                 f"da matriz, não confirmam a filial"), True
        return CanalEmpresa.Confianca.MEDIA, CanalEmpresa.AplicaA.GRUPO, \
            (f"site publica outro CNPJ da mesma raiz {empresa.cnpj[:8]} – possível vínculo com o grupo, não confirma "
             f"este estabelecimento; contatos tratados como do grupo"), True
    s = sinais_identidade(empresa, res.paginas)
    if not s["nome"]:
        # Nome fantasia diferente da razão social (inclusive razão = nome do titular) não impede a associação:
        # valem telefone, endereço, município e nome citado junto com o CNPJ por outra fonte. Nunca só o nome.
        alt = _nome_alt_no_site(nomes_alt, res.paginas, dominio_de_url(res.paginas[0][0] if res.paginas else ""))
        endereco = (s["cep"] and (s["logradouro"] or s["municipio"])) or (s["logradouro"] and s["municipio"])
        partes = [k for k, v in (("telefone cadastral", s["telefone"]), ("CEP", s["cep"]),
                                 ("logradouro", s["logradouro"]), ("município", s["municipio"])) if v]
        if alt:
            partes.append(f"nome '{alt}' citado com o mesmo CNPJ por outra fonte")
        if cnpjs:
            return CanalEmpresa.Confianca.CONFLITO, CanalEmpresa.AplicaA.DESCONHECIDO, \
                (f"Possível site da empresa – publica CNPJ de outro grupo ({', '.join(sorted(cnpjs))[:60]}); "
                 f"evidências: {', '.join(partes) or 'nenhuma'} – revisar"), False
        if endereco and (s["telefone"] or alt):
            return CanalEmpresa.Confianca.MEDIA, CanalEmpresa.AplicaA.EMPRESA, \
                (f"site sem a razão social, mas com evidências combinadas: {', '.join(partes)} ({como})"), True
        if s["telefone"] and alt:
            return CanalEmpresa.Confianca.MEDIA, CanalEmpresa.AplicaA.EMPRESA, \
                (f"site sem a razão social, mas com evidências combinadas: {', '.join(partes)} ({como})"), True
        if endereco or s["telefone"] or alt:
            return CanalEmpresa.Confianca.BAIXA, CanalEmpresa.AplicaA.DESCONHECIDO, \
                (f"Possível site da empresa – evidências parciais: {', '.join(partes)}; vínculo não confirmado, "
                 f"revisar ({como})"), False
        return None
    achados = [k for k in ("telefone", "cep", "logradouro", "municipio") if s[k]]
    if cnpjs:
        return CanalEmpresa.Confianca.CONFLITO, CanalEmpresa.AplicaA.DESCONHECIDO, \
            (f"site menciona o nome mas publica CNPJ de outro grupo ({', '.join(sorted(cnpjs))[:60]}) – revisar"), False
    confiavel = como.startswith("domínio do e-mail") or como.startswith("site já conhecido")
    descr = "nome" + ("".join(f" + {k}" for k in achados))
    if s["telefone"] or s["cep"] or (s["logradouro"] and s["municipio"]):
        nota_cnpj = ""
        if publicado and not empresa.cnpj:
            nota_cnpj = f"; o site publica o CNPJ {', '.join(sorted(publicado))[:40]}"
        return CanalEmpresa.Confianca.MEDIA, CanalEmpresa.AplicaA.EMPRESA, \
            f"evidências combinadas: {descr}{nota_cnpj} ({como})", True
    if confiavel:
        return CanalEmpresa.Confianca.MEDIA, CanalEmpresa.AplicaA.EMPRESA, \
            f"site de origem confiável menciona o nome ({descr}; {como})", True
    return CanalEmpresa.Confianca.BAIXA, CanalEmpresa.AplicaA.DESCONHECIDO, \
        f"Possível site da empresa – só {descr}, sem CNPJ/telefone/CEP; vínculo não confirmado, revisar ({como})", False


def _pista_bate(empresa, p):
    """A pista (diretório/resultado não lido) se refere a esta empresa? CNPJ igual, ou nome distintivo + município."""
    from carteira.services.normalizacao import normalizar_texto
    from prospeccao.services.identificacao import tokens_distintivos
    if _digitos(p.get("cnpj_citado")) and _digitos(p.get("cnpj_citado")) != empresa.cnpj:
        return False
    if _digitos(p.get("cnpj_citado")) == empresa.cnpj:
        return True
    nome = normalizar_texto(p.get("nome_citado") or p.get("titulo") or "")
    tokens = {t for t in tokens_distintivos(empresa.razao_social or empresa.nome) |
              tokens_distintivos(empresa.nome_fantasia) if len(t) >= 4}
    cid = normalizar_texto(p.get("municipio_citado") or "")
    return bool(tokens) and any(f" {t} " in f" {nome} " for t in tokens) and \
        (not cid or cid == normalizar_texto(empresa.cidade or ""))


def registrar_pistas(empresa, pistas, site_associado_dom="", leitura=None):
    """Contatos citados por diretórios ou por resultados cujo site não foi lido/confirmado: BAIXA (revisar), nunca
    utilizáveis automaticamente. Nada é deduzido: só o que a pesquisa diz estar publicado na URL indicada.
    `leitura` = {dominio: resultado da leitura} – o motivo da pendência vai na evidência."""
    n, leitura = 0, leitura or {}
    for p in pistas:
        if not (p.get("telefones") or p.get("emails")):
            continue
        if not _pista_bate(empresa, p):
            continue
        dom = p.get("dominio")
        if p.get("diretorio"):
            tipo_fonte = "diretório"
        elif dom == site_associado_dom:
            tipo_fonte = "página do site associado (citado pela pesquisa web, não encontrado pelo leitor do site)"
        else:
            tipo_fonte = "resultado de busca não confirmado no site"
        motivo = leitura.get(dom, "")
        evid = (f"citado em {tipo_fonte} {dom} ({p.get('verificacao', '')})"
                + (f"; leitura: {motivo}" if motivo else "") + " – não confirma a identidade; revisar antes de usar")
        for t in p.get("telefones", []):
            if registrar(empresa, CanalEmpresa.Tipo.TELEFONE, t, fonte=f"pista:{p.get('dominio')}"[:60], url=p["url"],
                         origem=CanalEmpresa.Origem.PUBLICACAO, confianca=CanalEmpresa.Confianca.BAIXA,
                         aplica_a=CanalEmpresa.AplicaA.DESCONHECIDO, evidencia=evid,
                         departamento=p.get("departamento", ""),
                         finalidade=ROTULO_FINALIDADE.get((p.get("departamento") or "").lower())):
                n += 1
        for e in p.get("emails", []):
            if registrar(empresa, CanalEmpresa.Tipo.EMAIL, e, fonte=f"pista:{p.get('dominio')}"[:60], url=p["url"],
                         origem=CanalEmpresa.Origem.PUBLICACAO, confianca=CanalEmpresa.Confianca.BAIXA,
                         aplica_a=CanalEmpresa.AplicaA.DESCONHECIDO, evidencia=evid,
                         departamento=p.get("departamento", "")):
                n += 1
    return n


def investigar_contatos(empresa, site_conector, ctx, buscar_fn=None, consultas=(), max_buscas=3, max_sites=4,
                        forcar_busca=False, max_complementares=1):
    """
    Procura o site correto e extrai canais. Retorna dict com páginas lidas (para evidências/IA) e o resultado.
    Sites prováveis (BAIXA) e conflitos ficam registrados para revisão, sem extrair contatos; a procura continua.
    Caminho GRATUITO primeiro (site conhecido, domínio do e-mail); a pesquisa web PAGA só é chamada se ele não
    associar um site com telefone ou e-mail. `forcar_busca` (empresa de CONTROLE do piloto): ignora o caminho
    gratuito, para medir se a pesquisa reencontra o que já se sabe.
    """
    from carteira.models import DadoColetado
    from prospeccao.services.enriquecimento import registrar_dado
    qualificar_cadastrais(empresa)
    resultado = {"site": None, "paginas": [], "tentativas": [], "novos_canais": 0, "revisar": [], "leitura": {},
                 "aplica_a": ""}
    pistas = []
    leitura = resultado["leitura"]
    antes_total = empresa.canais.count()
    vistos = set()
    resultado["nomes_alt"] = []
    gratis = [] if forcar_busca else _unicos(candidatos_gratuitos(empresa), vistos)
    resultado["busca_paga"] = "não chamada"
    _tentar_sites(empresa, site_conector, ctx, gratis[:max_sites], resultado, leitura)
    tem_contato = resultado["site"] and resultado.get("contatos_no_site", 0) > 0
    if buscar_fn is not None and not tem_contato:
        meta = {}
        pagos = _unicos(candidatos_busca(empresa, buscar_fn, consultas, max_buscas, pistas=pistas, meta=meta), vistos)
        resultado["nomes_alt"] = nomes_citados_com_cnpj(empresa, pistas)
        resultado["busca_paga"] = "chamada" + (" (controle: caminho gratuito ignorado)" if forcar_busca else "")
        for p in pistas:
            if p.get("diretorio"):
                leitura.setdefault(p.get("dominio"), "diretório/rede social: não lido pelo sistema (dados só como pista)")
        for url, como in pagos[max_sites:]:
            leitura.setdefault(dominio_de_url(url), f"não lido (limite de {max_sites} sites por empresa)")
        _tentar_sites(empresa, site_conector, ctx, pagos[:max_sites], resultado, leitura)
        # A IA analisou o retorno: se ainda falta contato, UMA consulta complementar escolhida por ela
        an = meta.get("analise") or {}
        tem_pista = any((p.get("telefones") or p.get("emails")) and _pista_bate(empresa, p) for p in pistas)
        proximas = [q for q in an.get("proximas_consultas") or [] if q not in meta.get("consultas", [])][:2]
        if not (resultado.get("contatos_no_site") or tem_pista) and proximas and max_complementares > 0:
            motivo = (f"IA analisou o retorno de '{' | '.join(meta.get('consultas', []))[:80]}': falta "
                      f"{', '.join(an.get('faltando') or ['contato'])} – {an.get('justificativa', '')}")[:300]
            resultado["complementar"] = {"motivo": motivo, "consultas": proximas}
            if ctx is not None and hasattr(ctx, "log"):
                ctx.log(f"{empresa.nome}: consulta complementar {proximas} ({motivo[:160]})", fonte="investigacao")
            antes_p = len(pistas)
            resultado["nomes_alt"] = nomes_citados_com_cnpj(empresa, pistas)
            pagos2 = _unicos(candidatos_busca(empresa, buscar_fn, proximas, len(proximas), pistas=pistas, meta={},
                                              motivo=motivo), vistos)
            for p in pistas[antes_p:]:
                if p.get("diretorio"):
                    leitura.setdefault(p.get("dominio"), "diretório/rede social: não lido pelo sistema (pista)")
            _tentar_sites(empresa, site_conector, ctx, pagos2[:max_sites], resultado, leitura)
            novos_p = sum(len(p.get("telefones") or []) + len(p.get("emails") or []) for p in pistas[antes_p:])
            resultado["complementar"]["resultado"] = (
                f"{len(pagos2)} site(s) candidato(s), {novos_p} telefone(s)/e-mail(s) citados; site associado: "
                f"{resultado.get('site') or 'nenhum'}")
            resultado["busca_paga"] += " + 1 complementar escolhida pela IA"
    elif buscar_fn is not None:
        resultado["busca_paga"] = "dispensada: caminho gratuito associou site com contato"
    if not resultado["site"]:
        DadoColetado.objects.update_or_create(
            empresa=empresa, campo="complemento_site", fonte="site_publico",
            defaults={"valor": "site não encontrado: " + "; ".join(resultado["tentativas"])[:450] or "nenhum candidato",
                      "coletado_em": timezone.now()})
    resultado["pistas"] = registrar_pistas(empresa, pistas, dominio_de_url(resultado["site"] or ""), leitura)
    resultado["novos_canais"] = empresa.canais.count() - antes_total
    return resultado


def pagina_lista_varias_empresas(texto, cnpjs, dominio):
    from prospeccao.conectores.busca_web import eh_diretorio
    if dominio and eh_diretorio(dominio):
        return True
    return len({(c or "")[:8] for c in cnpjs if c}) >= 2


def _posicao_no_texto(texto, valor, email=False):
    if email:
        return (texto or "").lower().find((valor or "").lower())
    digitos = re.sub(r"\D", "", valor or "")
    if len(digitos) < 8:
        return -1
    achados = list(_RE_TEL_PAGINA.finditer(texto or ""))
    for m in achados:
        if re.sub(r"\D", "", m.group(0)).endswith(digitos[-8:]):
            return m.start()
    return (texto or "").find(digitos)


_RE_TEL_PAGINA = re.compile(r"\(?\d{2}\)?\s?9?\d{4}[-.\s]?\d{4}")


def selecionar_contatos_do_texto(empresa, texto, telefones, emails, cnpjs, dominio=""):
    """Numa página com várias empresas, o contato só vale no trecho que cita esta."""
    from prospeccao.services.identificacao_cnpj import tokens_que_identificam

    if not pagina_lista_varias_empresas(texto, cnpjs, dominio):
        return list(telefones), [], list(emails), []
    toks = tokens_que_identificam(empresa)
    doc = re.sub(r"\D", "", getattr(empresa, "cnpj", None) or "")

    def cabe(pos):
        if pos < 0:
            return False
        anteriores = [p for p in posicoes if p < pos]
        inicio = anteriores[-1] if anteriores else max(0, pos - 240)
        trecho = normalizar_pagina(texto[inicio:pos])
        palavras = set(trecho.split())
        if doc and doc in re.sub(r"\D", "", texto[inicio:pos + 20]):
            return True
        return bool(toks) and any(t in palavras for t in toks)

    posicoes = []
    mapa = []
    for tel in telefones:
        pos = _posicao_no_texto(texto, tel, email=False)
        mapa.append(("tel", tel, pos))
        if pos >= 0:
            posicoes.append(pos)
    for email in emails:
        pos = _posicao_no_texto(texto, email, email=True)
        mapa.append(("email", email, pos))
        if pos >= 0:
            posicoes.append(pos)
    posicoes.sort()
    aceitos_t, rejeitados_t, aceitos_e, rejeitados_e = [], [], [], []
    for tipo, valor, pos in mapa:
        ok = cabe(pos)
        if tipo == "tel":
            (aceitos_t if ok else rejeitados_t).append(valor)
        else:
            (aceitos_e if ok else rejeitados_e).append(valor)
    return aceitos_t, rejeitados_t, aceitos_e, rejeitados_e


def normalizar_pagina(texto):
    from carteira.services.normalizacao import normalizar_texto
    return normalizar_texto(texto or "")


def _tentar_sites(empresa, site_conector, ctx, candidatos, resultado, leitura):
    from prospeccao.services.enriquecimento import registrar_dado
    for url, como in candidatos:
        d = dominio_de_url(url)
        if resultado["site"]:
            leitura.setdefault(d, "não lido (site da empresa já associado)")
            continue
        try:
            res = site_conector.analisar(url, ctx)
        except Exception as e:
            from prospeccao.conectores.base import ExecucaoCancelada, OrcamentoEsgotado
            if isinstance(e, (OrcamentoEsgotado, ExecucaoCancelada)):
                raise
            resultado["tentativas"].append(f"{url}: {type(e).__name__}")
            leitura[d] = f"falha do leitor do site ({type(e).__name__}) – pendente"
            continue
        if not res.paginas:
            resultado["tentativas"].append(f"{url}: não lido ({'; '.join(res.erros)[:120]})")
            leitura[d] = f"não lido ({'; '.join(res.erros)[:120]}) – pendente"
            continue
        assoc = associar_site(empresa, res, como, nomes_alt=resultado.get("nomes_alt", ()))
        if not assoc:
            leitura[d] = "lido: não é da empresa (não menciona o nome)"
            resultado["tentativas"].append(f"{url}: não é da empresa (não menciona o nome)")
            registrar_dado(empresa, f"site_rejeitado:{dominio_de_url(url)}"[:60], "não associado", "site_publico", url)
            continue
        confianca, aplica_a, evid, usar = assoc
        if not usar and confianca == CanalEmpresa.Confianca.CONFLITO:
            registrar(empresa, CanalEmpresa.Tipo.SITE, res.paginas[0][0], fonte="site_publico", url=res.paginas[0][0],
                      origem=CanalEmpresa.Origem.PUBLICACAO, confianca=confianca, aplica_a=aplica_a, evidencia=evid)
            resultado["revisar"].append(f"{res.paginas[0][0]}: {evid}")
            resultado["tentativas"].append(f"{url}: {evid[:120]}")
            leitura[d] = f"lido: associação {confianca} – {evid[:160]} (conflito; contatos não extraídos)"
            continue
        if not usar:
            leitura[d] = f"lido: associação {confianca} – {evid[:160]} (contatos gravados para conferência)"
        registrar(empresa, CanalEmpresa.Tipo.SITE, res.paginas[0][0], fonte="site_publico", url=res.paginas[0][0],
                  origem=CanalEmpresa.Origem.PUBLICACAO, confianca=confianca, aplica_a=aplica_a, evidencia=evid,
                  finalidade="COMERCIAL")
        dom = dominio_de_url(res.paginas[0][0])
        texto_pagina = "\n".join(t for _, t in res.paginas)
        tels_ok, tels_nao, emails_ok, emails_nao = selecionar_contatos_do_texto(
            empresa, texto_pagina, list(res.telefones), list(res.emails), list(res.cnpjs), dom)
        if tels_nao or emails_nao:
            from prospeccao.services.enriquecimento import registrar_dado
            registrar_dado(
                empresa, "contatos_rejeitados_pagina",
                ("rejeitados por não estarem no trecho desta empresa: "
                 + ", ".join(list(tels_nao) + list(emails_nao))[:400]),
                "site_publico", res.paginas[0][0])
        for email in emails_ok:
            u = res.emails.get(email, res.paginas[0][0])
            dom_email = email.split("@")[1]
            if dom_email != dom and not dom_email.endswith("." + dom):
                continue  # e-mail de outro domínio citado na página (parceiro, desenvolvedor...) não é associado
            rot = res.rotulos.get(email, "")
            registrar(empresa, CanalEmpresa.Tipo.EMAIL, email, fonte="site_publico", url=u,
                      origem=CanalEmpresa.Origem.PUBLICACAO, confianca=confianca, aplica_a=aplica_a, evidencia=evid,
                      departamento=rot, finalidade=ROTULO_FINALIDADE.get(rot))
        for tel in tels_ok:
            if tel in res.whatsapp:
                continue
            u = res.telefones.get(tel, res.paginas[0][0])
            rot = res.rotulos.get(tel, "")
            registrar(empresa, CanalEmpresa.Tipo.TELEFONE, tel, fonte="site_publico", url=u,
                      origem=CanalEmpresa.Origem.PUBLICACAO, confianca=confianca, aplica_a=aplica_a, evidencia=evid,
                      departamento=rot, finalidade=ROTULO_FINALIDADE.get(rot))
        wa_ok, wa_nao, _, _ = selecionar_contatos_do_texto(
            empresa, texto_pagina, list(res.whatsapp), [], list(res.cnpjs), dom)
        for w in wa_ok:
            u = res.whatsapp.get(w, res.paginas[0][0])
            registrar(empresa, CanalEmpresa.Tipo.TELEFONE, w, fonte="site_publico", url=u,
                      origem=CanalEmpresa.Origem.PUBLICACAO, confianca=confianca, aplica_a=aplica_a,
                      evidencia=evid + f"; link de WhatsApp publicado {u}", whatsapp=True)
        for link in getattr(res, "links_whatsapp", []) or []:
            registrar_dado(empresa, "link_whatsapp_publicado", str(link)[:300], "site_publico", res.paginas[0][0])
        if confianca == CanalEmpresa.Confianca.ALTA and not empresa.site:
            empresa.site, empresa.dominio = res.paginas[0][0][:300], dom
            empresa.save(update_fields=["site", "dominio", "atualizado_em"])
        resultado.update(site=res.paginas[0][0], paginas=res.paginas, confianca=confianca, como=como,
                         aplica_a=aplica_a, contatos_no_site=len(res.telefones) + len(res.emails))
        leitura[d] = (f"lido ({len(res.paginas)} página(s)): associação {confianca}/{aplica_a} – {evid[:160]}; "
                      f"{len(res.telefones)} telefone(s), {len(res.emails)} e-mail(s) na página")
        if getattr(res, "diagnostico", None):
            leitura[d] += "; leitura " + ",".join(x.get("classe", "") for x in res.diagnostico[:4])
        registrar_dado(empresa, "complemento_site", f"{confianca}: {evid}", "site_publico", res.paginas[0][0])
        from prospeccao.conectores.site import trecho_relevante
        trecho = trecho_relevante(texto_pagina, list(res.cnpjs))
        if trecho:
            registrar_dado(empresa, "trecho_pagina", trecho[:1500], "site_publico", res.paginas[0][0])
        if getattr(res, "emails_revisar", None):
            registrar_dado(
                empresa, "email_publicado_a_revisar",
                "; ".join(list(res.emails_revisar)[:6])[:400], "site_publico", res.paginas[0][0])


def terceiro_q():
    """Canal que não é contato direto da empresa: contador, compartilhado com outros grupos ou de outra empresa."""
    from django.db.models import Q
    return (Q(aplica_a=CanalEmpresa.AplicaA.CONTADOR) | Q(classificacao=CanalEmpresa.Classificacao.CONTADOR)
            | Q(confianca_associacao=CanalEmpresa.Confianca.CONFLITO) | ~Q(compartilhado=""))


def papel_terceiro(c):
    """'' (contato direto) | 'Contador' (evidência suficiente) | 'Contato de terceiro — possível contador'."""
    eh = (c.aplica_a == CanalEmpresa.AplicaA.CONTADOR or c.classificacao == CanalEmpresa.Classificacao.CONTADOR
          or bool(c.compartilhado) or c.confianca_associacao == CanalEmpresa.Confianca.CONFLITO)
    if not eh:
        return ""
    texto = f"{c.valor} {c.evidencia_associacao}".lower()
    if _CONTADOR.search(c.valor.split("@")[-1] if "@" in c.valor else "") or "escritório contábil" in texto \
            or "contabil" in texto:
        return "Contador"
    if c.confianca_associacao == CanalEmpresa.Confianca.CONFLITO and "outra(s) empresa" in texto:
        return "Contato de terceiro — publicado para outra empresa"
    return "Contato de terceiro — possível contador"


def canais_utilizaveis(empresa):
    """Telefones/e-mails DIRETOS que a equipe pode usar: cadastrais e de diretório entram (a conferir); ficam de fora
    restritos, incorretos, mascarados e contatos de terceiros (contador/compartilhado/outra empresa), que são
    mostrados à parte. Falta de confirmação não bloqueia."""
    return (empresa.canais.filter(tipo__in=[CanalEmpresa.Tipo.TELEFONE, CanalEmpresa.Tipo.EMAIL], restrito=False)
            .exclude(confirmacao_humana=CanalEmpresa.Confirmacao.INCORRETO)
            .exclude(terceiro_q())
            .exclude(classificacao="FISCAL"))


def canais_terceiros(empresa):
    return (empresa.canais.filter(tipo__in=[CanalEmpresa.Tipo.TELEFONE, CanalEmpresa.Tipo.EMAIL], restrito=False)
            .exclude(confirmacao_humana=CanalEmpresa.Confirmacao.INCORRETO).filter(terceiro_q()))
