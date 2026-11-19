"""
Conferência ANTES de executar (botão Executar e execuções agendadas). Nada é consultado na rede aqui.

impedimentos: sem eles resolvidos a execução não roda – a tela informa na hora o que falta, qual etapa fica
              impedida e como resolver (em vez de rodar minutos e terminar "Concluída" sem ter pesquisado).
avisos:       a execução roda, mas o resultado tem uma limitação conhecida (ex.: sem carteira local).
"""
import os

from django.db.models import Sum
from django.utils import timezone

from carteira.services.normalizacao import normalizar_texto
from prospeccao.models import linhas
from prospeccao.services.regioes import ABC_PAULISTA

# nomes oficiais com acento (OpenStreetMap compara o nome exato; a Receita guarda sem acento)
OFICIAIS = ABC_PAULISTA + ["Guarulhos", "São Paulo", "Osasco", "Barueri", "Mogi das Cruzes", "Suzano", "Itaquaquecetuba",
                           "Arujá", "Poá", "Ferraz de Vasconcelos", "Cotia", "Taboão da Serra", "Embu das Artes",
                           "Carapicuíba", "Santana de Parnaíba", "Jundiaí", "Campinas", "Santos", "São Vicente",
                           "Cubatão", "Praia Grande", "Guarujá", "Sorocaba", "São José dos Campos"]


_CACHE_CONHECIDOS = {}  # carga.pk -> nomes; a lista de municípios de uma carga não muda


def _conhecidos():
    """Nomes oficiais + municípios da carga ativa da Receita. Usa a lista gravada na própria carga
    (filtro_municipios) e guarda em memória: varrer os milhões de estabelecimentos a cada nome deixava a tela de
    nova pesquisa minutos carregando."""
    nomes = {normalizar_texto(n): n for n in OFICIAIS}
    try:
        from receita.importador import tabela
        from receita.models import CargaReceita, EstabelecimentoReceita
        carga = CargaReceita.ativa_atual()
        if carga:
            if carga.pk not in _CACHE_CONHECIDOS:
                cods = {str(m.get("codigo")) for m in (carga.filtro_municipios or []) if m.get("codigo")}
                if not cods:  # carga sem filtro gravado: consulta uma vez só (fica em memória)
                    cods = set(EstabelecimentoReceita.objects.filter(carga=carga)
                               .values_list("municipio_codigo", flat=True).distinct()[:5000])
                t = tabela(carga.competencia, "MUNICIPIO")
                _CACHE_CONHECIDOS.clear()
                _CACHE_CONHECIDOS[carga.pk] = {normalizar_texto(t[c]): t[c].title() for c in cods if t.get(c)}
            for chave, n in _CACHE_CONHECIDOS[carga.pk].items():
                nomes.setdefault(chave, n)
    except Exception:
        pass
    return nomes


def normalizar_municipios(nomes):
    """(oficiais, corrigidos, desconhecidos). Corrige maiúsculas/acentos e nome incompleto com UMA opção só
    (ex.: "São caetano" -> "São Caetano do Sul"). Desconhecido é mantido como digitado."""
    conhecidos = _conhecidos()
    saida, corrigidos, desconhecidos = [], [], []
    for bruto in nomes:
        chave = normalizar_texto(bruto)
        if not chave:
            continue
        oficial = conhecidos.get(chave)
        if oficial is None:
            opcoes = [n for k, n in conhecidos.items() if k.startswith(chave + " ")]
            oficial = opcoes[0] if len(opcoes) == 1 else None
        if oficial is None:
            desconhecidos.append(bruto.strip())
            oficial = bruto.strip()
        elif oficial != bruto.strip():
            corrigidos.append((bruto.strip(), oficial))
        if oficial not in saida:
            saida.append(oficial)
    return saida, corrigidos, desconhecidos


def municipios_ambiguos(nomes):
    """[(digitado, [opções])] quando o nome digitado corresponde a mais de um município: o usuário escolhe."""
    conhecidos = _conhecidos()
    saida = []
    for bruto in nomes:
        chave = normalizar_texto(bruto)
        if chave and chave not in conhecidos:
            opcoes = sorted(n for k, n in conhecidos.items() if k.startswith(chave + " "))
            if len(opcoes) > 1:
                saida.append((bruto.strip(), opcoes))
    return saida


def objetivo_padrao(frentes, cidades, uf):
    perfis = ", ".join(f.nome for f in frentes) or "perfis comerciais da BRIDA"
    return (f"Empresas de {perfis} em {', '.join(cidades)}/{uf}, fora da carteira Salesforce, com potencial de "
            f"compra de lubrificantes, graxas, filtros e autopeças.")


def teto_padrao():
    """Orçamento padrão PREVIAMENTE autorizado (PROSPECCAO_TETO_PADRAO_USD). Sem valor configurado: nenhum."""
    from decimal import Decimal, InvalidOperation
    v = (os.getenv("PROSPECCAO_TETO_PADRAO_USD") or "").strip()
    try:
        return Decimal(v) if v else None
    except InvalidOperation:
        return None


_CAMPOS_CONTA = (
    "plan_name", "plan_id", "searches_per_month", "plan_searches_left", "extra_credits",
    "total_searches_left", "this_month_usage", "this_hour_searches", "account_rate_limit_per_hour",
    "account_status",
)


def em_teste():
    import sys
    return "test" in sys.argv


def franquia_serpapi():
    """Saldo real da conta SerpApi (endpoint de conta: NÃO consome busca). None em teste ou sem chave."""
    if em_teste():
        return None
    return _ler_conta_serpapi()


def _ler_conta_serpapi():
    import requests
    chave = (os.getenv("SERPAPI_API_KEY") or "").strip()
    if not chave:
        return {"erro": "sem chave"}
    try:
        r = requests.get("https://serpapi.com/account.json", params={"api_key": chave}, timeout=8)
    except Exception as e:
        return {"erro": type(e).__name__}
    if r.status_code != 200:
        return {"erro": f"HTTP {r.status_code}"}
    try:
        d = r.json()
    except ValueError:
        return {"erro": "resposta inválida"}
    return {k: d.get(k) for k in _CAMPOS_CONTA}


def vagas_google_alheias(execucao_id=None):
    """Buscas já reservadas por outras execuções ativas. Não é um teto numérico."""
    from prospeccao.models import Execucao
    qs = Execucao.objects.filter(status__in=Execucao.ATIVOS)
    if execucao_id:
        qs = qs.exclude(pk=execucao_id)
    return sum(1 for e in qs.only("parametros") if (e.parametros or {}).get("vaga_google"))


def exigir_vaga_google(ctx):
    """Ocupa uma busca da franquia incluída. Não consulta max_consultas_dia nem um número substituto."""
    if em_teste():
        return
    from django.db import transaction
    from prospeccao.conectores.base import FonteIndisponivel, OrcamentoEsgotado
    from prospeccao.models import Execucao, Fonte
    ex = getattr(ctx, "execucao", None)
    with transaction.atomic():
        Fonte.objects.select_for_update().filter(codigo="google_serpapi").first()
        conta = _ler_conta_serpapi()
        if not conta or conta.get("erro"):
            raise OrcamentoEsgotado(
                "não foi possível ler a franquia SerpApi; nenhuma busca nova foi feita. "
                "Não é esgotamento da franquia.")
        if conta.get("account_status") not in (None, "", "Active"):
            raise OrcamentoEsgotado(f"conta SerpApi não está ativa ({conta.get('account_status')})")
        incluidas = int(conta.get("plan_searches_left") or 0)
        em_voo = vagas_google_alheias(getattr(ex, "pk", None))
        hora_lim = conta.get("account_rate_limit_per_hour")
        hora_uso = int(conta.get("this_hour_searches") or 0)
        if hora_lim and hora_uso + em_voo >= int(hora_lim):
            raise FonteIndisponivel(
                f"limite de velocidade SerpApi nesta hora ({hora_uso + em_voo}/{int(hora_lim)}). "
                "A franquia incluída não acabou.",
                retry_after=900)
        if incluidas - em_voo <= 0:
            if incluidas <= 0:
                raise OrcamentoEsgotado(
                    "franquia SerpApi incluída esgotada "
                    f"(plano {conta.get('plan_name')}, buscas do plano restantes 0). "
                    "Não é o teto de IA e não houve compra de crédito.")
            raise OrcamentoEsgotado(
                "franquia SerpApi restante já reservada por outra execução em andamento")
        if ex is not None:
            params = dict(ex.parametros or {})
            params["vaga_google"] = True
            Execucao.objects.filter(pk=ex.pk).update(parametros=params)
            ex.parametros = params


def liberar_vaga_google(ctx):
    if em_teste():
        return
    from prospeccao.models import Execucao
    ex = getattr(ctx, "execucao", None)
    if ex is None or not (ex.parametros or {}).get("vaga_google"):
        return
    params = dict(ex.parametros or {})
    params["vaga_google"] = False
    Execucao.objects.filter(pk=ex.pk).update(parametros=params)
    ex.parametros = params


def reserva_identificacao(pesquisa):
    """Uma busca dirigida por empresa já encontrada sem CNPJ e por vaga ainda aberta na meta.

    O número sai das lacunas e da quantidade definida. Não é um teto diário.
    """
    from django.db.models import Q
    from prospeccao.models import SelecaoPesquisa
    fora = ["CONHECIDA", "FORA", "NAO_CONTATAR", "JA_PESQUISADA"]
    base = SelecaoPesquisa.objects.filter(pesquisa=pesquisa, empresa__isnull=False).exclude(fila__in=fora)
    ja = base.count()
    sem_cnpj = base.filter(Q(empresa__cnpj__isnull=True) | Q(empresa__cnpj="")).count()
    meta = int(pesquisa.quantidade_desejada or 0)
    ainda = max(0, meta - ja)
    return sem_cnpj + ainda


def espaco_livre_gb():
    """Espaço livre no disco onde o Docker guarda os bancos (padrão: o disco do perfil do usuário)."""
    import shutil
    alvo = os.getenv("PROSPECCAO_DISCO_VERIFICAR") or os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
    try:
        return shutil.disk_usage(alvo).free / 1024 ** 3, alvo
    except OSError:
        return None, alvo


def disco_minimo_gb():
    try:
        return float(os.getenv("PROSPECCAO_DISCO_MIN_GB") or 3)
    except ValueError:
        return 3.0


def exigir_espaco(trabalho):
    """Para trabalhos pesados (backup, restauração, importação): levanta RuntimeError com mensagem clara."""
    livre, alvo = espaco_livre_gb()
    if livre is not None and livre < disco_minimo_gb():
        raise RuntimeError(f"Espaço em disco insuficiente para {trabalho}: {livre:.1f} GB livres em {alvo[:3]} "
                           f"(mínimo {disco_minimo_gb():.0f} GB). Libere espaço antes de continuar.")


def _imp(falta, etapa, como):
    return {"falta": falta, "etapa": etapa, "como": como}


def verificar(pesquisa):
    """(impedimentos, avisos)."""
    r = conferir(pesquisa)
    return r["impedimentos"], r["avisos"]


def conferir(pesquisa):
    """{impedimentos, avisos, info}: info = saldo de orçamento, limites e franquia, mostrado antes de executar."""
    from prospeccao.models import Fonte, UsoFonte
    from prospeccao.services import cruzamento as cz
    imp, avisos, info = [], [], []
    livre, alvo = espaco_livre_gb()
    if livre is not None and livre < disco_minimo_gb():
        imp.append(_imp(f"Espaço em disco: só {livre:.1f} GB livres em {alvo[:3]} (mínimo {disco_minimo_gb():.0f} GB)",
                        "todas as etapas – o banco (PostgreSQL no Docker) para de gravar com o disco cheio",
                        "Libere espaço no disco antes de executar"))
    elif livre is not None:
        info.append(f"Disco: {livre:.1f} GB livres em {alvo[:3]}")
    cidades = linhas(pesquisa.cidades)
    if not cidades:
        imp.append(_imp("Nenhum município informado", "descoberta", "Editar pesquisa → Municípios"))
    _, corrigidos, desconhecidos = normalizar_municipios(cidades)
    if corrigidos:
        imp.append(_imp("Nome de município fora da grafia oficial: " + "; ".join(f"“{a}” → “{b}”" for a, b in corrigidos),
                        "descoberta (o mapa e a conferência de região comparam o nome oficial)",
                        "Abra Editar pesquisa e salve: a grafia oficial é aplicada automaticamente"))
    if desconhecidos:
        avisos.append("Município não reconhecido na lista local (confira a grafia): " + ", ".join(desconhecidos))

    provedor = (os.getenv("PROSPECCAO_BUSCA_PROVEDOR") or "").strip().lower()
    if pesquisa.modo == "PLANEJADA":
        if not (pesquisa.objetivo or "").strip():
            imp.append(_imp("Objetivo da pesquisa", "plano", "Editar pesquisa → O que queremos encontrar"))
        if not pesquisa.frentes.exists():
            imp.append(_imp("Frente comercial", "descoberta por frente", "Editar pesquisa → selecione ao menos uma frente"))
        if provedor == "google":
            if not os.getenv("SERPAPI_API_KEY"):
                imp.append(_imp("Credencial do Google (SERPAPI_API_KEY) não configurada", "descoberta Google",
                                "Gestor configura a chave no arquivo .env do servidor"))
            fonte = Fonte.objects.filter(codigo="google_serpapi").first()
            hoje = 0
            if fonte is not None:
                hoje = UsoFonte.objects.filter(fonte=fonte, dia=timezone.localdate()).aggregate(
                    n=Sum("consultas"))["n"] or 0
            info.append(
                f"Google registrado hoje neste sistema: {hoje} requisição(ões). "
                "Não há teto diário interno. O limite é a franquia incluída da conta SerpApi.")
            fr = franquia_serpapi()
            if em_teste():
                fr = None
            if fr and "erro" not in fr:
                incluidas = int(fr.get("plan_searches_left") or 0)
                from prospeccao.services.descoberta_google import busca_reserva_identificacao
                if incluidas <= 0 and busca_reserva_identificacao(pesquisa) is not None:
                    from prospeccao.services.descoberta_google import max_descoberta_reserva
                    n_desc = max_descoberta_reserva()
                    avisos.append(
                        f"Franquia SerpApi incluída esgotada (plano {fr.get('plan_name')}): a busca segue "
                        "automaticamente pela busca web da OpenAI, dentro do teto financeiro — "
                        + (f"até {n_desc} consulta(s) de empresas novas por execução e " if n_desc else "")
                        + "1 busca por empresa para identificar o CNPJ.")
                elif incluidas <= 0:
                    imp.append(_imp(
                        f"Franquia SerpApi incluída esgotada (plano {fr.get('plan_name')}, "
                        f"buscas do plano restantes 0)",
                        "descoberta Google",
                        "Aguarde a renovação do plano já contratado. Não comprar crédito nem mudar de plano."))
                else:
                    info.append(
                        f"Franquia incluída ({fr.get('plan_name')}): {incluidas} de "
                        f"{fr.get('searches_per_month')} buscas restantes no ciclo; "
                        f"usadas no mês {fr.get('this_month_usage')}; "
                        f"nesta hora {fr.get('this_hour_searches')} de {fr.get('account_rate_limit_per_hour')}.")
                    if int(fr.get("extra_credits") or 0) > 0:
                        avisos.append(
                            "A conta tem créditos extras. Esta busca não os usa: só a franquia incluída do plano.")
            elif fr:
                imp.append(_imp(
                    f"Não foi possível ler a franquia SerpApi ({fr['erro']})",
                    "descoberta Google",
                    "A busca não começa sem o saldo real da conta. Não é um teto diário de 20."))
            from prospeccao.services import orcamento as orcm
            tg = orcm.teto_global()
            gasto_g = orcm.gasto_registrado(None)
            gasto_p = orcm.gasto_registrado(pesquisa)
            preco_google = (os.getenv("SERPAPI_PRECO_POR_BUSCA_USD") or "0.025").strip()
            info.append(
                f"Google/SerpApi: US$ {preco_google} por busca. Preço zero ainda consome uma requisição da franquia; "
                "não é chamada ilimitada e não é o orçamento de IA.")
            proprio = pesquisa.teto_custo_usd
            if tg is not None and gasto_g >= tg:
                imp.append(_imp(f"Teto global de IA atingido (US$ {tg})", "investigação por IA",
                                "Requer nova autorização: teto adicional de IA em US$ e, se a franquia acabou, "
                                "quantidade adicional de requisições SerpApi"))
            elif proprio and proprio > 0 and gasto_p >= proprio:
                imp.append(_imp(f"Teto de custo de IA desta pesquisa já consumido (US$ {proprio})",
                                "investigação por IA", "Requer nova autorização de orçamento desta pesquisa"))
            elif proprio and proprio > 0:
                info.append(f"Orçamento de IA desta pesquisa: teto US$ {proprio}, já registrado US$ {gasto_p:.4f}, "
                            f"disponível US$ {max(proprio - gasto_p, 0):.4f}"
                            + (f"; saldo global restante US$ {max(tg - gasto_g, 0):.4f}" if tg else ""))
            elif tg is not None and gasto_g < tg:
                info.append(
                    f"Esta pesquisa não tem teto próprio. Vale o saldo de IA já autorizado: US$ {tg}, "
                    f"registrado US$ {gasto_g:.4f}, disponível US$ {max(tg - gasto_g, 0):.4f}. Não é ilimitado.")
            else:
                imp.append(_imp(
                    "Teto de custo de IA ausente (pesquisa vazia e sem saldo global restante)",
                    "investigação por IA. A franquia Google é outro limite e não foi o motivo deste bloqueio",
                    "Editar pesquisa → informe um teto em US$ dentro da autorização vigente, ou solicite "
                    "teto adicional de IA e a quantidade adicional de requisições SerpApi"))
        elif not provedor:
            avisos.append("Nenhum motor de busca configurado (PROSPECCAO_BUSCA_PROVEDOR); só a base Receita será usada")
    else:
        fontes = list(pesquisa.fontes.filter(habilitada=True, tipo=Fonte.Tipo.DESCOBERTA))
        if not fontes:
            imp.append(_imp("Fonte de descoberta", "descoberta", "Editar pesquisa → Onde procurar"))
        for f in fontes:
            if not f.credencial_configurada:
                imp.append(_imp(f"Credencial da fonte {f.nome}", "descoberta", "Gestor configura a credencial"))
            if f.codigo == "osm_overpass" and not (pesquisa.termos_busca or "").strip() \
                    and not (pesquisa.filtros_osm or "").strip():
                imp.append(_imp("Palavras ou categorias para o OpenStreetMap", "descoberta OpenStreetMap",
                                "Editar pesquisa → Palavras no nome / categorias do mapa"))
        if provedor == "google":
            avisos.append("Busca direta usa só as fontes marcadas (" + ", ".join(f.nome for f in fontes) + "). "
                          "O Google é usado na Busca planejada (objetivo + frentes + teto).")
    atual, quando, horas = cz.cruzamento_atual()
    from carteira.models import Empresa
    n_sf = Empresa.objects.filter(salesforce_id__isnull=False).count()
    if quando and n_sf:
        dias = (timezone.now() - quando).days
        (info if atual else avisos).append(
            f"Carteira Salesforce local: {n_sf} contas, sincronizada em {timezone.localtime(quando):%d/%m/%Y %H:%M}"
            + ("" if atual else f" (há {dias} dia(s), não é recente): o sistema tenta atualizar no início e, se o "
                                f"Salesforce falhar, usa esta carteira para separar conhecidos"))
    elif n_sf:
        avisos.append(f"Carteira Salesforce local: {n_sf} contas já gravadas, carga ainda em andamento. "
                      "O cruzamento separa as já carregadas; as que faltam não serão tratadas como ausência confirmada.")
    else:
        avisos.append("Sem carteira Salesforce local: as empresas encontradas ficam com “Cruzamento pendente” e não são "
                      "confirmadas como novas até a carteira voltar")
    return {"impedimentos": imp, "avisos": avisos, "info": info}


def texto_impedimentos(imp):
    return " | ".join(f"Falta: {i['falta']}. Impede: {i['etapa']}. Como resolver: {i['como']}." for i in imp)
