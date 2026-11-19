"""
- Frentes comerciais iniciais (EDITÁVEIS no admin; não são uma classificação fechada).
  CNAEs são prefixos de referência; o plano confere cada um na tabela oficial da Receita.
- Fonte de busca web (Brave Search API) – exige BRAVE_SEARCH_API_KEY; sem ela fica "não configurada".
- Canais existentes: dimensões novas (origem, confiança na associação, aplica-se a, confirmação humana).
"""
from django.db import migrations

NEG_COMUM = ("financiamento\nfinanciar\nfinanciamos\ncredito\nconsorcio\nseguro\nseguros\nseguradora\ncotacao\n"
             "para sua frota\npara frotas\npara transportadoras\npara empresas de\natendemos\nnossos clientes\n"
             "clientes como\nrepresentante\nrevendedor\ndistribuidor autorizado\nvenda de\nvendemos\nnoticia\nblog")

FRENTES = [
    ("Transporte e operações com frota",
     "Empresas que operam veículos próprios: transportadoras, ônibus/fretamento, coleta de resíduos, logística, "
     "entregas, locadoras e operações com frota própria (inclusive de empresas de outros setores).",
     "4930\n4921\n4922\n4923002\n4924\n4929901\n4929902\n4929999\n3811\n3812\n3821\n5211701\n5211799\n5212\n"
     "5250803\n5250804\n5250805\n5320202\n7711\n7719599",
     "transport\nlogistic\ncargas\nfrete\nonibus\nviacao\nturismo\nfretamento\ncoleta\nambiental\nresiduos\n"
     "entregas\nexpress\nmudancas\nlocadora\nfrota\nrodoviario",
     "frota propria\ncaminhoes\nonibus\nveiculos\ncarretas\ngaragem\noficina propria\nmanutencao da frota\n"
     "centro de distribuicao\nmotoristas", NEG_COMUM,
     ["Lubrificantes Embalado", "Filtros", "Graxas - Brida", "Arla", "Arla Granel", "Aditivo de Radiador"]),
    ("Indústria e manutenção",
     "Indústrias e prestadores de manutenção industrial com máquinas, redutores, compressores, sistemas hidráulicos "
     "ou linhas de produção (inclusive têxtil, alimentícia, plástica, metalúrgica).",
     "10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n21\n22\n23\n24\n25\n26\n27\n28\n29\n30\n31\n32\n33",
     "industria\nmetalurg\nusinagem\nfabrica\nplastic\ntextil\nfundicao\nestamparia\ntornearia\ncaldeiraria\n"
     "embalagens\nmanutencao industrial\nmaquinas",
     "maquinas\nlinha de producao\nparque fabril\ncompressores\nredutores\nhidraulica\ninjetoras\nprensas\n"
     "teares\ntornos\nmanutencao industrial\nempilhadeiras", NEG_COMUM,
     ["Lubrificantes Embalado", "Graxas - Brida", "Lubs (Equipamentos) - Brida", "Filtros"]),
    ("Agro, máquinas e serviços relacionados",
     "Produtores rurais, serviços agrícolas, granjas, paisagismo e empresas que operam tratores, implementos, "
     "irrigação ou máquinas agrícolas. Atender o agro (crédito, consultoria) não comprova operação agrícola.",
     "01\n02\n03\n0161\n0162\n0163\n4661\n7731\n8130",
     "agro\nagricola\nfazenda\nsitio\nrural\npecuaria\navicultura\ngranja\ntrator\nplantio\nirrigacao\npaisagismo",
     "tratores\ncolheitadeira\nimplementos\nplantio\ncolheita\nrocadeira\nirrigacao\npulverizador\nmaquinas agricolas",
     NEG_COMUM + "\ncredito rural\nseguro agricola\nconsultoria",
     ["Lubrificantes Embalado", "Graxas - Brida", "Filtros", "Lubs (Equipamentos) - Brida"]),
    ("Construção e equipamentos",
     "Construtoras, terraplenagem, pavimentação, mineração e locação de máquinas e equipamentos pesados.",
     "41\n42\n43\n0810\n7732\n7739",
     "construtora\nconstrucao\nengenharia\nterraplenagem\npavimentacao\nconcreto\nguindastes\nmineracao\nlocacao",
     "escavadeira\nretroescavadeira\nbetoneira\nguindaste\nmaquinas pesadas\ngeradores\ncompressores\ncaminhoes",
     NEG_COMUM, ["Lubrificantes Embalado", "Graxas - Brida", "Filtros", "Arla", "Lubs (Equipamentos) - Brida"]),
    ("Revenda e manutenção automotiva",
     "Oficinas, centros automotivos, autopeças, postos e serviços automotivos que revendem ou aplicam produtos.",
     "4520\n4530\n4541\n4542\n4543\n4731\n4732",
     "auto\nmecanica\noficina\npecas\ncenter\nposto\npneus\nretifica\nfunilaria\nmoto",
     "troca de oleo\noficina\nrevisao\nelevadores\nmecanica\nlubrificacao", NEG_COMUM.replace("\nvenda de\nvendemos", ""),
     ["Lubrificantes Embalado", "Filtros", "Aditivo de Radiador", "Fluido para Freio", "Palhetas Para Brisa"]),
]


def aplicar(apps, schema_editor):
    Frente = apps.get_model("prospeccao", "FrenteComercial")
    Cat = apps.get_model("prospeccao", "CategoriaProduto")
    Fonte = apps.get_model("prospeccao", "Fonte")
    Canal = apps.get_model("carteira", "CanalEmpresa")
    cats = {c.nome: c for c in Cat.objects.all()}
    for nome, desc, cnaes, termos, sinais, neg, apl in FRENTES:
        f, _ = Frente.objects.update_or_create(nome=nome, defaults=dict(
            descricao=desc, cnaes_referencia=cnaes, termos_operacao=termos, sinais_operacionais=sinais,
            contextos_negativos=neg, ativa=True))
        f.aplicacoes.set([cats[a] for a in apl if a in cats])
    Fonte.objects.update_or_create(codigo="busca_web_brave", defaults=dict(
        nome="Busca web – Brave Search API", tipo="IDENTIFICACAO", habilitada=True,
        endpoints="https://api.search.brave.com/res/v1/web/search", intervalo_minimo_segundos=1.1,
        timeout_segundos=20, max_consultas_execucao=90, max_consultas_dia=1000, max_retentativas=2,
        variavel_credencial="BRAVE_SEARCH_API_KEY",
        termos_uso_url="https://api-dashboard.search.brave.com/app/documentation/web-search/get-started",
        licenca="Termos da Brave Search API (plano contratado)",
        dados_permitidos="Somente URLs/títulos dos resultados para localizar o site oficial da empresa; o conteúdo "
                         "só é lido do próprio site (respeitando robots.txt). Diretórios agregadores não são lidos.",
        observacoes="Sem BRAVE_SEARCH_API_KEY a busca web fica 'não configurada'."))
    mapa = {
        "CADASTRAL": dict(origem_canal="CADASTRAL", confianca_associacao="MEDIA", aplica_a="ESTABELECIMENTO"),
        "CONFIRMADO_FONTE": dict(origem_canal="PUBLICACAO", confianca_associacao="ALTA", aplica_a="EMPRESA"),
        "ENCONTRADO": dict(origem_canal="PUBLICACAO", confianca_associacao="MEDIA", aplica_a="DESCONHECIDO"),
        "CONFLITO": dict(origem_canal="PUBLICACAO", confianca_associacao="CONFLITO", aplica_a="DESCONHECIDO"),
        "CONFIRMADO_HUMANO": dict(origem_canal="HUMANO", confianca_associacao="ALTA", aplica_a="EMPRESA",
                                  confirmacao_humana="ATENDIDO"),
    }
    for v, campos in mapa.items():
        Canal.objects.filter(verificacao=v).update(**campos)
    for c in Canal.objects.filter(valor_normalizado=""):
        c.valor_normalizado = c.valor
        c.save(update_fields=["valor_normalizado"])


class Migration(migrations.Migration):
    dependencies = [("prospeccao", "0005_pesquisa_aplicacoes_pesquisa_criterios_obrigatorios_and_more"),
                    ("carteira", "0004_canalempresa_aplica_a_canalempresa_compartilhado_and_more")]
    operations = [migrations.RunPython(aplicar, migrations.RunPython.noop)]
