"""
Dados iniciais (editáveis depois no admin):
  - Fontes documentadas e seus limites
  - Catálogo: categorias REAIS vendidas (InvoiceItem__c.TypeofProdutct__c, diagnóstico de 05/11/2026)
  - Regras de evidência transparentes (frota, máquinas, coleta, agro, construção, oficinas...)
"""
from django.db import migrations

FONTES = [
    dict(codigo="osm_overpass", nome="OpenStreetMap (Overpass API)", tipo="DESCOBERTA",
         endpoints="https://maps.mail.ru/osm/tools/overpass/api/interpreter\n"
                   "https://overpass-api.de/api/interpreter\nhttps://overpass.kumi.systems/api/interpreter",
         intervalo_minimo_segundos=5, timeout_segundos=90, max_consultas_execucao=20, max_consultas_dia=200,
         max_retentativas=2, licenca="ODbL 1.0 – atribuição © OpenStreetMap contributors",
         termos_uso_url="https://operations.osmfoundation.org/policies/api/",
         atribuicao="© OpenStreetMap contributors",
         dados_permitidos="Id do objeto, nome, endereço, telefone, site, e-mail e etiquetas de categoria, com link "
                          "para o objeto. Base ODbL: pode ser armazenada com atribuição.",
         observacoes="Instâncias públicas: uso justo, baixo volume e uma consulta por vez. Para uso recorrente "
                     "intenso, a recomendação oficial é instância própria (troque os endpoints)."),
    dict(codigo="brasilapi_cnpj", nome="BrasilAPI – CNPJ (dados abertos da Receita)", tipo="ENRIQUECIMENTO",
         endpoints="https://brasilapi.com.br/api/cnpj/v1/", intervalo_minimo_segundos=1.5, timeout_segundos=30,
         max_consultas_execucao=150, max_consultas_dia=1500, max_retentativas=2,
         licenca="Dados públicos do CNPJ (Receita Federal, dados abertos)",
         termos_uso_url="https://brasilapi.com.br/docs#tag/CNPJ",
         dados_permitidos="Dados cadastrais públicos do CNPJ: razão social, fantasia, CNAEs, abertura, situação, "
                          "endereço, telefone/e-mail cadastrais, quadro societário.",
         observacoes="Só consulta CNPJ conhecido (identificação/enriquecimento); não descobre empresas."),
    dict(codigo="site_publico", nome="Site público da empresa", tipo="IDENTIFICACAO",
         intervalo_minimo_segundos=1.5, timeout_segundos=15, max_consultas_execucao=160, max_retentativas=1,
         licenca="Conteúdo do próprio site; respeita robots.txt",
         dados_permitidos="CNPJ, telefones e e-mails publicados e trechos curtos (até 300 caracteres) com a URL. "
                          "A página inteira não é armazenada.",
         observacoes="Até 3 páginas por site (início, contato, sobre). Proteções contra endereços internos."),
    dict(codigo="csv_importacao", nome="Importação de arquivo CSV", tipo="IMPORTACAO", intervalo_minimo_segundos=0,
         max_consultas_execucao=0, dados_permitidos="Linhas fornecidas pelo usuário",
         observacoes="Alternativa operacional. NÃO é descoberta automática."),
]

CATEGORIAS = [
    ("Lubrificantes Embalado", 57547), ("Filtros", 45366), ("Arla Granel", 6040), ("Aditivo de Radiador", 2748),
    ("Palhetas Para Brisa", 2671), ("Cuidados Automotivos - Brida", 2140), ("Troca Inteligente(Lub)", 2097),
    ("Peças Motos", 1653), ("Manutenção Automotiva", 1648), ("Arla", 1515), ("Água desmineralizada - Brida", 1013),
    ("Militec", 840), ("Fluido p/ Radiador - Brida", 611), ("Pneus", 444), ("Fluido para Freio", 407),
    ("Aditivos - Brida", 212), ("Descarbonizante", 191), ("Água desmineralizada", 155), ("Fluido p/ Freio - Brida", 153),
    ("Especialidades", 94), ("Fluido p/ Radiador (Paraflu) - Brida", 68), ("Graxas - Brida", 50),
    ("Lubs (Equipamentos) - Brida", 23), ("Disco Tacografo - Brida", 17), ("Solúvel", 13), ("Tintas (Spray)", 9),
    ("Arla (Equipamentos) - Brida", 4), ("Protetivo", 2),
]

LUB, FIL, GRX, ARLA, ARLAG, ADT, FRE, PAL, EQP = ("Lubrificantes Embalado", "Filtros", "Graxas - Brida", "Arla",
                                                  "Arla Granel", "Aditivo de Radiador", "Fluido para Freio",
                                                  "Palhetas Para Brisa", "Lubs (Equipamentos) - Brida")
P_FROTA = "Confirmar tamanho da frota, quem faz a manutenção e quem compra lubrificantes e filtros."
P_MAQ = "Confirmar quais máquinas/equipamentos operam, quem faz a manutenção e quem compra os insumos."
P_OFI = "Confirmar volume de serviços de troca/manutenção e fornecedor atual."

GRUPOS = [
    # (nome, tipo, padrões texto/nome, padrões categoria OSM, prefixos CNAE, aplicações, pergunta)
    ("Frota de caminhões / transporte de cargas", "FROTA",
     "frota\nfrota propria\ncaminhao\ncaminhoes\ncarreta\ncarretas\ncavalo mecanico\ntransporte de cargas\n"
     "transporte rodoviario\ntransportadora\nlogistica\ndistribuicao de cargas",
     "office logistics\nindustrial depot\nshop trucks", "4930\n5211\n5212\n5250",
     [LUB, FIL, GRX, ARLA, ARLAG, ADT], P_FROTA),
    ("Ônibus / transporte de passageiros", "FROTA",
     "onibus\nfretamento\ntransporte de passageiros\ntransporte coletivo\nviacao\nturismo rodoviario\ntransporte escolar",
     "amenity bus station\noffice transport", "4921\n4922\n4923\n4924\n4929", [LUB, FIL, GRX, ARLA, ADT], P_FROTA),
    ("Coleta de resíduos / limpeza urbana", "FROTA",
     "coleta de lixo\ncoleta de residuos\nresiduos solidos\nlimpeza urbana\ncaminhao compactador\ncompactadores\n"
     "aterro\nreciclagem\ncacamba\ncacambas\ngestao de residuos",
     "amenity recycling\namenity waste transfer station\nindustrial waste", "3811\n3812\n3821\n3822\n3831\n3839\n3900",
     [LUB, GRX, FIL, ARLA, EQP], "Confirmar quem realiza a manutenção dos caminhões e equipamentos e quem compra os insumos."),
    ("Máquinas agrícolas / agro", "MAQUINAS",
     "trator\ntratores\ncolheitadeira\ncolheitadeiras\nimplementos agricolas\nmaquinas agricolas\nplantio\ncolheita\n"
     "pulverizador\nagronegocio\nusina\nsucroalcooleiro",
     "landuse farmyard\nshop agrarian\nshop farm\nplace farm", "01\n02\n0161\n0162\n4661",
     [LUB, GRX, FIL, EQP], P_MAQ),
    ("Máquinas industriais / hidráulica / compressores / geradores", "MAQUINAS",
     "maquinas\nmaquinario\nequipamentos industriais\nhidraulico\nhidraulica\nhidraulicos\ncompressor\ncompressores\n"
     "gerador\ngeradores\ninjetora\ninjetoras\ntorno\ntornos\nusinagem\nprensa\nprensas\ntear\nteares\nempilhadeira\n"
     "empilhadeiras\nlinha de producao\nparque fabril\nmanutencao industrial",
     "landuse industrial\nman made works\nindustrial factory\nbuilding industrial\ncraft metal construction",
     "10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n21\n22\n23\n24\n25\n26\n27\n28\n29\n30\n31\n32\n33",
     [LUB, GRX, EQP, FIL], P_MAQ),
    ("Construção / terraplenagem / locação de máquinas", "MAQUINAS",
     "terraplenagem\nescavadeira\nescavadeiras\nretroescavadeira\npa carregadeira\nguindaste\nguindastes\nbetoneira\n"
     "maquinas pesadas\nlocacao de equipamentos\nlocacao de maquinas\npavimentacao\nmineracao\npedreira",
     "landuse construction\nlanduse quarry\ncraft builder", "41\n42\n43\n0810\n07\n7732\n7739",
     [LUB, GRX, FIL, ARLA, EQP], P_MAQ),
    ("Oficina / manutenção automotiva", "MANUTENCAO",
     "oficina\nmecanica\nmecanico\ntroca de oleo\nauto center\ncentro automotivo\nfunilaria\nretifica\n"
     "manutencao automotiva\nauto eletrica\nborracharia\nlava rapido",
     "shop car repair\nshop motorcycle repair\nshop tyres\ncraft car repair", "4520\n4543",
     [LUB, FIL, ADT, FRE, PAL], P_OFI),
    ("Autopeças / revenda de peças", "REVENDA",
     "autopecas\nauto pecas\nmotopecas\nmoto pecas\npecas automotivas\ndistribuidora de pecas\nacessorios automotivos",
     "shop car parts\nshop motorcycle", "4530\n4541\n4542", [LUB, FIL, ADT, FRE, PAL],
     "Confirmar linhas de produto revendidas e fornecedor atual."),
    ("Posto de combustível", "REVENDA",
     "auto posto\nposto de combustivel\nposto de gasolina\ncombustiveis",
     "amenity fuel", "4731\n4732", [LUB, FIL, ARLA, ADT], "Confirmar se faz troca de óleo e quem fornece."),
    ("Locação / frota de veículos leves", "FROTA",
     "locadora de veiculos\naluguel de carros\nlocacao de veiculos\ngestao de frotas",
     "amenity car rental", "7711\n7719", [LUB, FIL, ADT, FRE, PAL], P_FROTA),
]


def semear(apps, schema_editor):
    Fonte = apps.get_model("prospeccao", "Fonte")
    Cat = apps.get_model("prospeccao", "CategoriaProduto")
    Regra = apps.get_model("prospeccao", "RegraEvidencia")
    for f in FONTES:
        Fonte.objects.update_or_create(codigo=f["codigo"], defaults=f)
    cats = {}
    for nome, n in CATEGORIAS:
        cats[nome], _ = Cat.objects.update_or_create(nome=nome, defaults={"notas_com_venda": n,
                                                                          "origem": "Salesforce TypeofProdutct__c (diagnóstico 05/11/2026)"})
    for nome, tipo, texto, osm, cnae, apl, pergunta in GRUPOS:
        regras = [
            ("TEXTO_SITE", f"{nome} – site", texto, f"Site informa: {nome.lower()}"),
            ("NOME", f"{nome} – nome", texto, f"Nome sugere: {nome.lower()}"),
            ("ATIVIDADE_FONTE", f"{nome} – categoria da fonte", osm, f"Categoria na fonte: {nome.lower()}"),
            ("CNAE", f"{nome} – CNAE", cnae, f"CNAE sugere: {nome.lower()}"),
        ]
        for alvo, nome_regra, padroes, descricao in regras:
            r, _ = Regra.objects.update_or_create(nome=nome_regra, defaults=dict(
                alvo=alvo, padroes=padroes, tipo_evidencia=tipo, descricao=descricao[:255], pergunta_sugerida=pergunta,
                ativa=True))
            r.aplicacoes.set([cats[a] for a in apl if a in cats])


class Migration(migrations.Migration):
    dependencies = [("prospeccao", "0001_initial")]
    operations = [migrations.RunPython(semear, migrations.RunPython.noop)]
