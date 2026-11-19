from django import forms
from django.contrib.auth import get_user_model

from prospeccao.models import Candidato, Evidencia, Fonte, Oportunidade, Pesquisa
from prospeccao.services.cruzamento import Situacao
from prospeccao.widgets import ListaBusca, cnaes_oficiais, municipios_atuacao

TEXTAREA = forms.Textarea(attrs={"rows": 3})

SITUACOES_RECEITA = [("ATIVA", "Ativa"), ("SUSPENSA", "Suspensa"), ("INAPTA", "Inapta"), ("BAIXADA", "Baixada"),
                     ("NULA", "Nula")]

# Campos técnicos com valor padrão no modelo: ficam na seção recolhida e, em branco, voltam ao padrão.
CAMPOS_AVANCADOS = ["filtros_osm", "termos_operacao", "cnaes_filtro", "segmentos", "limite_consultas",
                    "identificar_por_site", "max_sites_execucao", "enriquecer_por_cnpj",
                    "frequencia_horas", "validade_dados_dias", "exclusoes_termos", "exclusoes_cnaes", "descricao", "ativa"]
NUMERICOS_COM_PADRAO = ["limite_resultados", "limite_consultas", "max_sites_execucao", "max_duracao_segundos",
                        "validade_dados_dias", "quantidade_desejada"]


class PesquisaForm(forms.ModelForm):
    situacoes_sel = forms.MultipleChoiceField(
        label="Situação cadastral na Receita", required=False, choices=SITUACOES_RECEITA,
        widget=ListaBusca(placeholder="Buscar situação", vazio="Só Ativa (padrão)"),
        help_text="Quais situações trazer da base Receita. Sem marcação, usa somente Ativa.")

    class Meta:
        model = Pesquisa
        fields = ["nome", "descricao", "ativa", "modo", "objetivo", "frentes", "aplicacoes", "criterios_obrigatorios",
                  "preferencias", "quantidade_desejada", "permitir_exploratoria",
                  "segmentos", "fontes", "cnaes_filtro", "situacoes_receita",
                  "termos_busca", "filtros_osm", "termos_operacao", "uf",
                  "cidades", "limite_resultados", "limite_consultas", "identificar_por_site",
                  "max_sites_execucao", "enriquecer_por_cnpj", "max_duracao_segundos", "teto_custo_usd",
                  "frequencia_horas", "validade_dados_dias", "idade_min_anos", "idade_max_anos", "idade_desconhecida",
                  "exclusoes_termos", "exclusoes_cnaes"]
        widgets = {k: TEXTAREA for k in ("descricao", "segmentos", "termos_busca", "filtros_osm", "termos_operacao",
                                         "cidades", "exclusoes_termos", "exclusoes_cnaes", "cnaes_filtro", "objetivo",
                                         "criterios_obrigatorios", "preferencias")}
        widgets["frentes"] = ListaBusca(placeholder="Buscar frente comercial", vazio="Nenhuma frente")
        widgets["aplicacoes"] = ListaBusca(placeholder="Buscar produto ou aplicação", vazio="Nenhum (opcional)")
        widgets["fontes"] = ListaBusca(placeholder="Buscar fonte", vazio="Nenhuma fonte")
        widgets["modo"] = forms.RadioSelect
        widgets["uf"] = forms.TextInput(attrs={"maxlength": 2, "autocomplete": "off", "style": "text-transform:uppercase"})
        labels = {
            "nome": "Nome da pesquisa", "descricao": "Descrição interna", "ativa": "Pesquisa ativa",
            "modo": "Como escolher as empresas", "objetivo": "O que queremos encontrar",
            "frentes": "Frentes comerciais", "aplicacoes": "Produtos e aplicações de interesse",
            "criterios_obrigatorios": "Critérios obrigatórios", "preferencias": "Preferências",
            "quantidade_desejada": "Novos leads no total da pesquisa", "permitir_exploratoria": "Permitir abordagem exploratória",
            "segmentos": "Segmentos (organização da busca)", "fontes": "Onde procurar",
            "cnaes_filtro": "Atividades (CNAE) na base Receita", "termos_busca": "Palavras no nome da empresa",
            "filtros_osm": "Categorias do mapa (OpenStreetMap)", "termos_operacao": "Termos de operação a procurar nos sites",
            "uf": "UF", "cidades": "Municípios", "limite_resultados": "Máximo de empresas por execução",
            "limite_consultas": "Consultas em fontes com teto próprio", "identificar_por_site": "Procurar CNPJ e evidências no site público",
            "max_sites_execucao": "Máximo de sites lidos por execução", "enriquecer_por_cnpj": "Completar dados pelo CNPJ",
            "max_duracao_segundos": "Duração máxima da execução (segundos)", "frequencia_horas": "Repetir automaticamente a cada (horas)",
            "validade_dados_dias": "Validade dos dados coletados (dias)", "idade_min_anos": "Tempo de empresa mínimo (anos)",
            "idade_max_anos": "Tempo de empresa máximo (anos)", "idade_desconhecida": "Se a data de abertura for desconhecida",
            "exclusoes_termos": "Excluir empresas com estes termos", "exclusoes_cnaes": "Excluir estas atividades (CNAE)",
        }
        help_texts = {
            "nome": "Ex.: Transportadoras e coleta de resíduos – Guarulhos.",
            "objetivo": "Em linguagem natural. Ex.: empresas com frota própria de caminhões ou máquinas que façam manutenção.",
            "frentes": "Organizam a busca. Não excluem empresas de outros segmentos com operação parecida.",
            "aplicacoes": "Opcional. Ajuda a explicar por que a empresa pode comprar.",
            "criterios_obrigatorios": "Um por linha. Ex.: situação ATIVA; ter telefone.",
            "preferencias": "Um por linha. Só mudam a ordem; ninguém é excluído por preferência.",
            "quantidade_desejada": "De 1 a 100. Vale para a pesquisa inteira, somando frentes e municípios.",
            "cidades": "Um município por linha. ABC Paulista vira os municípios do Grande ABC ao salvar.",
            "teto_custo_usd": "Orçamento de IA desta pesquisa, em dólar. Vazio usa o saldo global já autorizado, se houver. Não autoriza chamadas ilimitadas. A franquia Google é contada em requisições, mesmo quando o preço da busca é zero.",
            "max_duracao_segundos": "Tempo máximo desta execução. Ao estourar, o que já foi gravado permanece.",
            "permitir_exploratoria": "Inclui empresas com aderência provável pela atividade, mesmo sem evidência forte.",
            "cidades": "Marque os municípios. Para uma região (ex.: ABC Paulista) ou cidade fora da lista, digite na busca e tecle Enter.",
            "termos_busca": "Um por linha, sem diferenciar acentos. Ex.: transport, coleta, ônibus.",
            "fontes": "Selecione ao menos uma.",
            "limite_resultados": "Padrão: 200.",
            "limite_consultas": "Não é o teto do Google. A busca Google usa a franquia incluída da conta SerpApi. Este número limita outras fontes, como o OpenStreetMap.",
            "filtros_osm": "chave=valor, um por linha. Ex.: shop=car_repair, amenity=fuel, landuse=industrial.",
            "cnaes_filtro": "Atividade principal ou secundária. Nenhuma marcada = todas. Um prefixo (ex.: 49) pode ser digitado na busca e confirmado com Enter.",
            "segmentos": "Um por linha. Pode ser um segmento que ainda não existe na carteira.",
            "frequencia_horas": "Vazio = só quando alguém clicar em Executar. Exige agendamento ativo na configuração.",
            "exclusoes_termos": "Só o que o gestor decidir excluir. Um por linha.",
            "exclusoes_cnaes": "Só as atividades marcadas são excluídas. Nenhum setor é excluído automaticamente.",
        }

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["fontes"].queryset = Fonte.objects.filter(tipo=Fonte.Tipo.DESCOBERTA)
        self.fields["fontes"].required = False
        self.fields["frentes"].required = False
        self.fields["aplicacoes"].required = False
        self.fields["situacoes_receita"].required = False
        self.fields["cidades"].required = True
        self.fields["modo"].choices = [
            ("SIMPLES", "Busca direta – por atividade, palavras no nome ou categoria do mapa"),
            ("PLANEJADA", "Busca planejada – parte do objetivo e das frentes, com plano revisável e investigação"),
        ]
        for nome in NUMERICOS_COM_PADRAO:
            campo = self.fields[nome]
            campo.required = False
            padrao = Pesquisa._meta.get_field(nome).default
            campo.widget.attrs.setdefault("placeholder", str(padrao))
            campo.widget.attrs["min"] = 1
        self.fields["quantidade_desejada"].widget.attrs["max"] = 100
        self.fields["teto_custo_usd"].required = False
        self.expansao = []
        atual = (self.instance.situacoes_receita if self.instance and self.instance.pk else "ATIVA") or "ATIVA"
        self.fields["situacoes_sel"].initial = [s.strip().upper() for s in atual.split(",") if s.strip()]
        self.fields["uf"].widget.attrs["autocapitalize"] = "characters"
        # padrão da tela: lista com busca, Todos e Limpar (o texto gravado continua um valor por linha)
        self.fields["cidades"].widget = ListaBusca(
            choices=municipios_atuacao(), texto=True, permitir_novo=True, placeholder="Buscar município",
            vazio="Nenhum município")
        cnaes = cnaes_oficiais()
        for nome, vazio in (("cnaes_filtro", "Todas as atividades"), ("exclusoes_cnaes", "Nenhuma exclusão")):
            self.fields[nome].widget = ListaBusca(
                choices=cnaes, texto=True, permitir_novo=True, vazio=vazio,
                placeholder="Buscar CNAE por código ou descrição")

    SECAO_OBJETIVO = ["nome", "modo", "objetivo", "frentes", "aplicacoes"]
    SECAO_REGIAO = ["uf", "cidades", "situacoes_sel", "situacoes_receita", "fontes", "termos_busca", "limite_resultados",
                    "quantidade_desejada", "teto_custo_usd", "max_duracao_segundos", "criterios_obrigatorios",
                    "preferencias", "permitir_exploratoria", "idade_min_anos", "idade_max_anos", "idade_desconhecida"]

    def _erro(self, nomes):
        return self.is_bound and any(n in self.errors for n in nomes)

    @property
    def erro_objetivo(self):
        return self._erro(self.SECAO_OBJETIVO)

    @property
    def erro_regiao(self):
        return self._erro(self.SECAO_REGIAO) or self._erro(CAMPOS_AVANCADOS)

    @property
    def erro_avancado(self):
        return self._erro(CAMPOS_AVANCADOS)

    def avancados(self):
        return [self[n] for n in CAMPOS_AVANCADOS]

    def clean(self):
        d = super().clean()
        from prospeccao.models import linhas
        for nome in NUMERICOS_COM_PADRAO:
            if d.get(nome) in (None, ""):
                d[nome] = Pesquisa._meta.get_field(nome).default
        from prospeccao.services.regioes import expandir
        nomes, avisos = expandir(linhas(d.get("cidades")))
        self.expansao = avisos
        if not nomes:
            self.add_error("cidades", "Informe ao menos um município ou uma região conhecida, como ABC Paulista.")
        else:
            from prospeccao.services.prontidao import municipios_ambiguos, normalizar_municipios
            for digitado, opcoes in municipios_ambiguos(nomes):  # não adivinha: o usuário escolhe
                self.add_error("cidades", f"“{digitado}” corresponde a mais de um município: {', '.join(opcoes)}. "
                                          f"Escreva o nome completo do que deseja.")
            nomes, corrigidos, _desc = normalizar_municipios(nomes)  # grafia oficial (mapa e região comparam o nome)
            self.expansao += [(a, [b]) for a, b in corrigidos]
            d["cidades"] = "\n".join(nomes)
        usa_osm = any(f.codigo == "osm_overpass" for f in (d.get("fontes") or []))
        if not d.get("fontes") and d.get("modo") != "PLANEJADA":  # planejada: Google + IA + Receita + carteira
            self.add_error("fontes", "Selecione ao menos uma fonte de descoberta.")
        if d.get("modo") == "PLANEJADA":
            if not d.get("frentes"):
                self.add_error("frentes", "Selecione ao menos um perfil (frente comercial).")
            elif not (d.get("objetivo") or "").strip():
                from prospeccao.services.prontidao import objetivo_padrao
                d["objetivo"] = objetivo_padrao(d["frentes"], linhas(d.get("cidades")), (d.get("uf") or "SP").upper())
        if usa_osm:
            if not (d.get("termos_busca") or "").strip() and not (d.get("filtros_osm") or "").strip():
                self.add_error("termos_busca", "OpenStreetMap exige palavras no nome ou categorias do mapa.")
            from prospeccao.conectores.osm import montar_consulta
            try:
                for cidade in linhas(d.get("cidades")) or ["X"]:
                    montar_consulta((d.get("uf") or "SP").upper(), cidade, linhas(d.get("termos_busca")),
                                    linhas(d.get("filtros_osm")), 10)
            except ValueError as e:
                self.add_error(None, str(e))
        if self.data.get("situacoes_ui"):
            sits = [s for s in (d.get("situacoes_sel") or [])]
        else:
            sits = [x.strip().upper() for x in (d.get("situacoes_receita") or "").split(",") if x.strip()]
        validas = {"ATIVA", "SUSPENSA", "INAPTA", "BAIXADA", "NULA"}
        if any(x not in validas for x in sits):
            self.add_error("situacoes_receita", "Use ATIVA, SUSPENSA, INAPTA, BAIXADA ou NULA, separadas por vírgula.")
        d["situacoes_receita"] = ",".join(sits) or "ATIVA"
        if d.get("quantidade_desejada") is not None and not 1 <= int(d["quantidade_desejada"]) <= 100:
            self.add_error("quantidade_desejada", "Informe de 1 a 100 novos leads no total da pesquisa.")
        if d.get("uf"):
            d["uf"] = d["uf"].upper()
        if d.get("idade_min_anos") is not None and d.get("idade_max_anos") is not None \
                and d["idade_min_anos"] > d["idade_max_anos"]:
            self.add_error("idade_max_anos", "O máximo deve ser maior ou igual ao mínimo.")
        return d


class FonteForm(forms.ModelForm):
    class Meta:
        model = Fonte
        fields = ["habilitada", "endpoints", "intervalo_minimo_segundos", "timeout_segundos", "max_consultas_execucao",
                  "max_consultas_dia", "max_retentativas", "custo_por_consulta", "variavel_credencial", "observacoes"]
        widgets = {"endpoints": TEXTAREA, "observacoes": TEXTAREA}


ETAPAS = [
    ("", "Em aberto"),
    ("conferir", "A conferir (novas)"),
    ("primeira", "Novos — ainda sem contato efetivo"),
    ("sem_abordagem", "Ainda sem contato efetivo"),
    ("disponiveis", "Disponíveis na lista da pesquisa"),
    ("validado", "Cadastro validado para venda"),
    ("completar_cnpj", "Completar CNPJ"),
    ("conferir_cadastro", "Conferir identidade ou situação cadastral"),
    ("conhecidas", "Conhecidas no Salesforce"),
    ("andamento", "Em acompanhamento (contato efetivo)"),
    ("retorno", "Com retorno pendente"),
    ("encerradas", "Encerradas (descartadas, bloqueadas, clientes)"),
    ("todas", "Todas"),
]


class FiltroOportunidadeForm(forms.Form):
    q = forms.CharField(label="Empresa ou CNPJ", required=False, widget=forms.TextInput(
        attrs={"type": "search", "placeholder": "Nome fantasia, razão social ou CNPJ", "autocomplete": "off"}))
    etapa = forms.ChoiceField(label="Etapa da prospecção", required=False, choices=ETAPAS)
    contato = forms.ChoiceField(label="Contato", required=False, choices=[
        ("", "Todos"), ("direto", "Com contato direto"), ("sem_direto", "Sem contato direto"),
        ("telefone", "Com telefone"), ("email", "Com e-mail"), ("sem", "Sem telefone nem e-mail")])
    responsavel = forms.ChoiceField(label="Responsável", required=False)
    cidade = forms.CharField(label="Município", required=False)
    # ---- mais filtros
    segmento = forms.CharField(label="Atividade ou segmento", required=False)
    escopo = forms.ChoiceField(label="Perfil comercial", required=False, choices=[
        ("", "No perfil + a revisar"), ("dentro", "Só no perfil"), ("todos", "Todos (inclui fora do perfil)"),
        ("FROTA", "Frota própria"), ("INDUSTRIA", "Indústria com máquinas"),
        ("AGRO_MAQUINAS", "Agro, construção e máquinas"), ("REVENDA_OFICINA", "Revenda e oficinas"),
        ("REVISAR", "A revisar (concorrente / atividade secundária)"), ("FORA", "Fora do perfil")])
    tipo = forms.ChoiceField(label="Situação no Salesforce", required=False, choices=[("", "Todas")] + list(Oportunidade.Tipo.choices))
    prioridade = forms.ChoiceField(label="Prioridade", required=False, choices=[("", "Todas")] + list(Oportunidade.Prioridade.choices))
    evidencia = forms.ChoiceField(label="Evidência de aplicação", required=False, choices=[("", "Todas")] + list(Evidencia.Classificacao.choices))
    pesquisa = forms.ModelChoiceField(label="Pesquisa de origem", queryset=Pesquisa.objects.all(), required=False,
                                      empty_label="Todas")
    alertas = forms.BooleanField(label="Só com alertas (tempo de empresa, raiz de cliente...)", required=False)
    # compatibilidade com links antigos (?status=..., ?abordagem=...)
    status = forms.ChoiceField(label="Situação da oportunidade", required=False,
                               choices=[("", "Conforme a etapa"), ("todas", "Todas")] + list(Oportunidade.Status.choices))
    abordagem = forms.ChoiceField(required=False, widget=forms.HiddenInput, choices=[
        ("", "Todas"), ("primeira", "Fila de primeira abordagem"), ("abordada", "Contato efetivo"),
        ("tentativa", "Tentativa sem resposta"), ("retorno", "Com retorno pendente")])

    PRINCIPAIS = ("q", "etapa", "contato", "responsavel", "cidade")
    MAIS = ("segmento", "escopo", "tipo", "prioridade", "evidencia", "pesquisa", "status", "alertas")

    def __init__(self, *a, usuario=None, **kw):
        super().__init__(*a, **kw)
        users = get_user_model().objects.filter(is_active=True).order_by("username")
        self.fields["responsavel"].choices = [("", "Todos"), ("meus", "Minhas"), ("nenhum", "Sem responsável")] + \
            [(str(u.pk), u.get_username()) for u in users]

    def principais(self):
        return [self[n] for n in self.PRINCIPAIS]

    def mais(self):
        return [self[n] for n in self.MAIS]

    def mais_ativos(self):
        if not self.is_bound:
            return 0
        return sum(1 for n in self.MAIS if self.data.get(n))


class FiltroCandidatoForm(forms.Form):
    status = forms.ChoiceField(label="Situação", required=False, choices=[("", "Todas as pendências")]
                               + list(Candidato.Status.choices))
    pesquisa = forms.ModelChoiceField(label="Pesquisa", queryset=Pesquisa.objects.all(), required=False, empty_label="Todas")
    q = forms.CharField(label="Nome ou município", required=False, widget=forms.TextInput(attrs={"type": "search"}))
    com_sugestao = forms.BooleanField(label="Só com possível correspondência na base", required=False)


class InformarCNPJForm(forms.Form):
    cnpj = forms.CharField(label="CNPJ", max_length=20)
    motivo = forms.CharField(label="Como foi confirmado", max_length=255,
                             help_text="Ex.: site oficial, ligação, nota fiscal.")


class MotivoForm(forms.Form):
    motivo = forms.CharField(max_length=255)


class PrioridadeForm(forms.Form):
    prioridade = forms.ChoiceField(label="Nova prioridade", choices=Oportunidade.Prioridade.choices)
    motivo = forms.CharField(label="Motivo", max_length=255)


class CSVForm(forms.Form):
    arquivo = forms.FileField(label="Arquivo CSV",
                              help_text="Colunas: nome (obrigatória), cnpj, cidade, uf, telefone, email, site, atividade, endereco")


SITUACAO_LABELS = dict(Situacao.choices)
