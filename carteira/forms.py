from datetime import date

from django import forms
from django.core.validators import validate_email
from django.utils import timezone

from carteira.models import (
    Abordagem, ConfiguracaoAnalise, Contato, Empresa, SituacaoCompra, SituacaoContato,
)
from carteira.services.indicadores import data_limite_inatividade


def _anos_atras(anos):
    hoje = date.today()
    try:
        return hoje.replace(year=hoje.year - anos)
    except ValueError:  # 29/02
        return hoje.replace(year=hoje.year - anos, day=28)


def _opcoes(campo):
    valores = (Empresa.objects.exclude(**{campo: ""}).order_by(campo)
               .values_list(campo, flat=True).distinct())
    return [("", "Todos")] + [(v, v) for v in valores] + [("__vazio__", "(não informado)")]


class FiltroEmpresaForm(forms.Form):
    q = forms.CharField(label="Busca (nome, CNPJ, código)", required=False)
    coordenador = forms.ChoiceField(label="Coordenador", required=False)
    segmento = forms.ChoiceField(required=False)
    ramo = forms.ChoiceField(required=False)
    uf = forms.ChoiceField(label="UF", required=False)
    cidade = forms.CharField(label="Município", required=False)
    vendedor = forms.ChoiceField(required=False)
    tempo_min = forms.IntegerField(label="Tempo de empresa ≥ (anos)", required=False, min_value=0)
    tempo_max = forms.IntegerField(label="Tempo de empresa ≤ (anos)", required=False, min_value=0)
    tempo_desconhecido = forms.BooleanField(label="Só com data de abertura desconhecida", required=False)
    situacao_compra = forms.ChoiceField(label="Situação de compra", required=False,
                                        choices=[("", "Todas")] + list(SituacaoCompra.choices))
    inativos = forms.BooleanField(label="Só compradores inativos", required=False)
    dias_inatividade = forms.IntegerField(label="Dias sem compra (inatividade)", required=False, min_value=1)
    situacao_contato = forms.ChoiceField(label="Situação de contato", required=False,
                                         choices=[("", "Todas")] + list(SituacaoContato.choices))
    revisao = forms.ChoiceField(label="Revisão cadastral", required=False,
                                choices=[("", "Todas"), ("sim", "Requer revisão"), ("nao", "Sem pendência")])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for campo in ("coordenador", "segmento", "ramo", "uf", "vendedor"):
            self.fields[campo].choices = _opcoes(campo)
        self.fields["dias_inatividade"].help_text = (
            f"Padrão configurado: {ConfiguracaoAnalise.obter().dias_inatividade or 'não definido'}"
        )

    def aplicar(self, qs):
        if not self.is_valid():
            return qs
        d = self.cleaned_data
        if d["q"]:
            from django.db.models import Q
            termo = d["q"].strip()
            from carteira.services.cnpj import normalizar
            filtro = Q(nome__icontains=termo) | Q(codigo_cliente__iexact=termo) | Q(razao_social__icontains=termo)
            if normalizar(termo):
                filtro |= Q(cnpj__startswith=normalizar(termo))
            qs = qs.filter(filtro)
        for campo in ("coordenador", "segmento", "ramo", "uf", "vendedor"):
            if d[campo] == "__vazio__":
                qs = qs.filter(**{campo: ""})
            elif d[campo]:
                qs = qs.filter(**{campo: d[campo]})
        if d["cidade"]:
            qs = qs.filter(cidade__icontains=d["cidade"])
        if d["tempo_desconhecido"]:
            qs = qs.filter(data_abertura__isnull=True)
        if d["tempo_min"] is not None:
            qs = qs.filter(data_abertura__lte=_anos_atras(d["tempo_min"]))
        if d["tempo_max"] is not None:
            qs = qs.filter(data_abertura__gt=_anos_atras(d["tempo_max"] + 1))
        if d["situacao_compra"]:
            qs = qs.filter(situacao_compra=d["situacao_compra"])
        if d["inativos"]:
            limite = data_limite_inatividade(d["dias_inatividade"])
            if limite is None:
                self.add_error("dias_inatividade", "Informe os dias ou configure o padrão no admin.")
            else:
                qs = qs.filter(situacao_compra=SituacaoCompra.COMPROU, indicador__ultima_compra__lt=limite)
        if d["situacao_contato"]:
            qs = qs.filter(situacao_contato=d["situacao_contato"])
        if d["revisao"] == "sim":
            qs = qs.filter(requer_revisao=True)
        elif d["revisao"] == "nao":
            qs = qs.filter(requer_revisao=False)
        return qs


class ValorReais(forms.CharField):
    """Aceita 1500 · 1500,50 · 1.500,50 · R$ 1.500,50 (formato brasileiro). Devolve Decimal ou None."""

    def to_python(self, value):
        texto = (super().to_python(value) or "").replace("R$", "").replace(" ", "").strip()
        if not texto:
            return None
        if "," in texto:
            texto = texto.replace(".", "").replace(",", ".")
        from decimal import Decimal, InvalidOperation
        try:
            return Decimal(texto).quantize(Decimal("0.01"))
        except InvalidOperation:
            raise forms.ValidationError("Informe o valor em reais, por exemplo 1.500,00.")


# Regras do registro de contato (o painel de captação depende delas):
RESULTADOS_TENTATIVA = {"SEM_RESPOSTA", "RECADO", "CONTATO_INVALIDO"}
RESULTADOS_IDENTIFICA = {"CONVERSA_INTERESSE", "CONVERSA_RETORNAR", "CONVERSA_SEM_INTERESSE",
                         "CONVERSA_ORCAMENTO", "CONVERSA_VENDA"}   # conversa comercial: CNPJ confirmado ou justificado


class AbordagemForm(forms.ModelForm):
    # aceita a máscara 00.000.000/0000-00; grava só os 14 dígitos (validados no clean)
    cnpj_confirmado = forms.CharField(required=False, max_length=20)
    sem_cnpj = forms.BooleanField(required=False, label="O cliente não informou o CNPJ nesta conversa")
    valor_orcamento = ValorReais(required=False)
    valor_venda = ValorReais(required=False)
    concluir_pendentes = forms.BooleanField(
        label="Concluir retornos pendentes desta empresa", required=False, initial=True,
    )
    justificativa_excecao = forms.CharField(
        label="Justificativa de exceção (somente gestor, quando a regra de nova tentativa bloquear)",
        required=False, widget=forms.TextInput,
    )
    # nova pessoa (o nome de quem atendeu), cadastrada junto com o contato
    pessoa_nome = forms.CharField(label="Nome", required=False, max_length=200)
    pessoa_cargo = forms.CharField(label="Cargo / função", required=False, max_length=120)
    pessoa_departamento = forms.ChoiceField(label="Departamento", required=False)
    pessoa_telefone = forms.CharField(label="Telefone / celular", required=False, max_length=60)
    pessoa_email = forms.EmailField(label="E-mail", required=False)

    class Meta:
        model = Abordagem
        fields = ["contato", "data_hora", "canal", "resultado", "cnpj_confirmado", "valor_orcamento", "anexo_orcamento",
                  "valor_venda", "observacoes", "proxima_acao", "data_retorno"]
        widgets = {
            "data_hora": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "data_retorno": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "observacoes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, empresa, **kwargs):
        super().__init__(*args, **kwargs)
        self.empresa = empresa
        self.fields["contato"].queryset = empresa.contatos.filter(ativo=True)
        self.fields["contato"].required = False
        self.fields["contato"].empty_label = "(pessoa não identificada)"
        self.fields["contato"].label_from_instance = lambda c: c.nome + (
            f" – {c.cargo}" if c.cargo else "") + (f" ({c.departamento})" if c.departamento else "")
        self.fields["pessoa_departamento"].choices = [("", "Selecione")] + [(x, x) for x in DEPARTAMENTOS]
        for campo, ph in (("pessoa_nome", "Nome de quem atendeu"), ("pessoa_cargo", "Ex.: comprador, gerente de frota"),
                          ("pessoa_telefone", "(11) 98765-4321"), ("pessoa_email", "nome@empresa.com.br")):
            self.fields[campo].widget.attrs.update({"placeholder": ph, "autocomplete": "off"})
        self.fields["data_hora"].input_formats = ["%Y-%m-%dT%H:%M"]
        rotulos = {
            "contato": ("Pessoa já cadastrada", "Escolha quem atendeu ou cadastre uma pessoa nova logo abaixo."),
            "data_hora": ("Data e hora do contato",
                          "Pode ser anterior a hoje. Na disputa do mês, o contato conta no dia em que é registrado."),
            "canal": ("Como foi o contato", ""),
            "resultado": ("Resultado", "Tentativa = não houve conversa. Conversa = falou com alguém da empresa. "
                                       "Em “orçamento enviado” aparecem o valor e o campo para anexar o orçamento."),
            "observacoes": ("O que foi conversado", "Interesse, produtos citados, objeções, quem decide a compra..."),
            "proxima_acao": ("Próximo passo combinado", "Ex.: enviar proposta de filtros; ligar para o comprador."),
            "data_retorno": ("Data de retorno", "Obrigatória quando o resultado for “Conversa – pediu retorno”."),
            "cnpj_confirmado": ("CNPJ confirmado com o cliente",
                                "Confira com o cliente (ou corrija). Quando a nota fiscal deste CNPJ chegar do "
                                "Salesforce, a venda e o valor entram no painel automaticamente, em nome de quem atendeu."),
            "valor_orcamento": ("Valor do orçamento (R$)", "Total do orçamento enviado. Entra no painel de captação."),
            "anexo_orcamento": ("Arquivo do orçamento",
                                "Obrigatório para orçamento enviado: PDF, imagem ou planilha (até 10 MB). "
                                "É o comprovante que conta na disputa do mês."),
            "valor_venda": ("Valor previsto da venda (R$)",
                            "Opcional. O valor oficial vem das notas fiscais do Salesforce para o CNPJ confirmado."),
        }
        for nome, (rot, ajuda) in rotulos.items():
            self.fields[nome].label = rot
            if ajuda:
                self.fields[nome].help_text = ajuda
        self.fields["concluir_pendentes"].label = "Concluir os retornos pendentes desta empresa"
        self.fields["concluir_pendentes"].help_text = "Marque se este contato já é o retorno que estava combinado."
        self.fields["justificativa_excecao"].label = "Justificativa de exceção (gestor)"
        self.fields["justificativa_excecao"].help_text = "Só quando a regra de nova tentativa bloquear o registro."
        rot = dict(self.fields["resultado"].choices)
        self.fields["resultado"].choices = [
            ("", "Selecione o resultado"),
            ("Tentativa – não houve conversa", [(k, rot[k].replace("Tentativa – ", "")) for k in
                                               ("SEM_RESPOSTA", "RECADO", "CONTATO_INVALIDO")]),
            ("Conversa – falou com alguém da empresa", [(k, rot[k].replace("Conversa – ", "")) for k in
                                                       ("CONVERSA_INTERESSE", "CONVERSA_RETORNAR", "CONVERSA_ORCAMENTO",
                                                        "CONVERSA_VENDA", "CONVERSA_SEM_INTERESSE",
                                                        "CONVERSA_NAO_CONTATAR")]),
        ]
        self.fields["canal"].choices = [("", "Selecione")] + [c for c in self.fields["canal"].choices if c[0]]
        for campo in ("valor_orcamento", "valor_venda"):
            self.fields[campo].widget.attrs.update({"inputmode": "decimal", "autocomplete": "off", "placeholder": "0,00"})
        if not empresa.cnpj:
            self.fields["cnpj_confirmado"].help_text = (
                "Pergunte o CNPJ ao cliente. Quando a nota fiscal deste CNPJ chegar do Salesforce, a venda e o valor "
                "entram no painel automaticamente, em nome de quem atendeu.")
        self.fields["cnpj_confirmado"].widget.attrs.update(
            {"inputmode": "numeric", "autocomplete": "off", "placeholder": "00.000.000/0000-00"})
        if not self.is_bound:
            self.initial["data_hora"] = timezone.localtime().strftime("%Y-%m-%dT%H:%M")
            # O CNPJ encontrado NÃO vem preenchido: confirmar é uma ação do atendente (sugestão na tela),
            # porque o CNPJ confirmado define a quem a venda por nota fiscal é atribuída.

    def clean(self):
        d = super().clean()
        res = d.get("resultado") or ""
        # data do contato: não pode estar no futuro (distorce o gráfico por dia)
        if d.get("data_hora") and d["data_hora"] > timezone.now() + __import__("datetime").timedelta(minutes=10):
            self.add_error("data_hora", "A data e hora do contato não pode estar no futuro.")
        # tentativa: não há CNPJ, orçamento nem venda — descarta o que ficou preenchido em campo escondido
        if res in RESULTADOS_TENTATIVA or res == "CONVERSA_NAO_CONTATAR":
            d["valor_orcamento"] = d["valor_venda"] = None
            if res in RESULTADOS_TENTATIVA:
                d["cnpj_confirmado"], d["sem_cnpj"] = "", False
        if res != "CONVERSA_VENDA":
            d["valor_venda"] = None
        if res == "CONVERSA_ORCAMENTO" and not d.get("valor_orcamento"):
            self.add_error("valor_orcamento", "Informe o valor do orçamento enviado.")
        if d.get("data_retorno") and d.get("data_hora") and d["data_retorno"] < timezone.localdate(d["data_hora"]):
            self.add_error("data_retorno", "A data de retorno não pode ser anterior à abordagem.")
        if d.get("resultado") == "CONVERSA_RETORNAR" and not d.get("data_retorno"):
            self.add_error("data_retorno", "Informe a data de retorno combinada.")
        bruto = (d.get("cnpj_confirmado") or "").strip()
        if bruto:
            from carteira.services.cnpj import cnpj_valido
            dig = "".join(ch for ch in bruto if ch.isdigit())
            if not cnpj_valido(dig):
                self.add_error("cnpj_confirmado", "CNPJ inválido. Confira os 14 números com o cliente.")
            else:
                d["cnpj_confirmado"] = dig
        for campo in ("valor_orcamento", "valor_venda"):
            if d.get(campo) is not None and d[campo] <= 0:
                self.add_error(campo, "O valor precisa ser maior que zero.")
        # conversa comercial: o CNPJ é confirmado com o cliente (liga a venda às notas fiscais)
        tem_cnpj = bool(d.get("cnpj_confirmado")) and "cnpj_confirmado" not in self.errors
        if res == "CONVERSA_VENDA" and not tem_cnpj and "cnpj_confirmado" not in self.errors:
            self.add_error("cnpj_confirmado", "Venda fechada exige o CNPJ confirmado com o cliente (liga às notas fiscais).")
        elif res in RESULTADOS_IDENTIFICA and not tem_cnpj and not d.get("sem_cnpj") \
                and "cnpj_confirmado" not in self.errors:
            self.add_error("cnpj_confirmado", "Confirme o CNPJ com o cliente ou marque que ele não informou.")
        if tem_cnpj:
            d["sem_cnpj"] = False
        # pessoa nova (nome do lead) cadastrada no próprio registro
        # orçamento: o arquivo do orçamento enviado é o comprovante (pré-requisito da gamificação)
        arq = d.get("anexo_orcamento")
        if res == "CONVERSA_ORCAMENTO" and not arq:
            self.add_error("anexo_orcamento", "Anexe o orçamento enviado ao cliente (PDF, imagem ou planilha).")
        if arq:
            from django.conf import settings
            ext = (arq.name.rsplit(".", 1)[-1] if "." in arq.name else "").lower()
            if ext not in EXTENSOES_ORCAMENTO:
                self.add_error("anexo_orcamento", "Formato não aceito. Use PDF, JPG, PNG, DOC(X), XLS(X) ou ODS/ODT.")
            elif arq.size > settings.ANEXO_MAX_MB * 1024 * 1024:
                self.add_error("anexo_orcamento", f"Arquivo maior que {settings.ANEXO_MAX_MB} MB.")
            if res not in ("CONVERSA_ORCAMENTO", "CONVERSA_VENDA"):
                d["anexo_orcamento"] = None  # campo escondido nos outros resultados
        if d.get("pessoa_nome") and d.get("contato"):
            self.add_error("pessoa_nome", "Escolha uma pessoa já cadastrada OU cadastre uma nova, não as duas.")
        if not d.get("pessoa_nome") and any(d.get(c) for c in ("pessoa_cargo", "pessoa_telefone", "pessoa_email")):
            self.add_error("pessoa_nome", "Informe o nome da pessoa.")
        if d.get("pessoa_telefone") and not _telefone_ok(d["pessoa_telefone"]):
            self.add_error("pessoa_telefone", "Telefone com DDD, por exemplo (11) 4002-8922 ou (11) 98765-4321.")
        # outros telefones e e-mails da empresa (vários): linhas extra_tipo / extra_valor / extra_depto
        self.extras, erros = ler_extras(self.data)
        for erro in erros:
            self.add_error(None, erro)
        return d



def ler_extras(data):
    """Linhas extra_tipo / extra_valor / extra_depto da tela -> (lista de canais válidos, mensagens de erro)."""
    def lista(chave):  # QueryDict (tela) ou dict simples (uso interno)
        if hasattr(data, "getlist"):
            return data.getlist(chave)
        v = data.get(chave, [])
        return list(v) if isinstance(v, (list, tuple)) else [v]
    extras, erros = [], []
    tipos, valores, deptos = lista("extra_tipo"), lista("extra_valor"), lista("extra_depto")
    for i, valor in enumerate(valores):
        valor = (valor or "").strip()
        if not valor:
            continue
        tipo = tipos[i] if i < len(tipos) else ""
        depto = (deptos[i] if i < len(deptos) else "").strip()[:60]
        if tipo == "EMAIL":
            try:
                validate_email(valor)
            except forms.ValidationError:
                erros.append(f"E-mail inválido em “Outros contatos”: {valor}")
                continue
        elif tipo == "TELEFONE":
            if not _telefone_ok(valor):
                erros.append(f"Telefone inválido em “Outros contatos”: {valor} (use DDD + número).")
                continue
        else:
            continue
        extras.append({"tipo": tipo, "valor": valor, "departamento": depto})
    return extras, erros


class DadosContatoForm(forms.Form):
    """Só a pessoa nova e os telefones/e-mails da empresa: salvar sem registrar o atendimento."""
    pessoa_nome = forms.CharField(required=False, max_length=200)
    pessoa_cargo = forms.CharField(required=False, max_length=120)
    pessoa_departamento = forms.CharField(required=False, max_length=60)
    pessoa_telefone = forms.CharField(required=False, max_length=60)
    pessoa_email = forms.EmailField(required=False, error_messages={"invalid": "E-mail da pessoa inválido."})

    def clean(self):
        d = super().clean()
        if not d.get("pessoa_nome") and any(d.get(c) for c in ("pessoa_cargo", "pessoa_telefone", "pessoa_email")):
            self.add_error(None, "Informe o nome da pessoa.")
        if d.get("pessoa_telefone") and not _telefone_ok(d["pessoa_telefone"]):
            self.add_error(None, "Telefone da pessoa com DDD, por exemplo (11) 4002-8922 ou (11) 98765-4321.")
        if d.get("pessoa_departamento") and d["pessoa_departamento"] not in DEPARTAMENTOS:
            d["pessoa_departamento"] = ""
        self.extras, erros = ler_extras(self.data)
        for erro in erros:
            self.add_error(None, erro)
        if not self.errors and not d.get("pessoa_nome") and not self.extras:
            self.add_error(None, "Nada para salvar: preencha a pessoa nova ou um telefone/e-mail.")
        return d


EXTENSOES_ORCAMENTO = {"pdf", "jpg", "jpeg", "png", "webp", "doc", "docx", "xls", "xlsx", "ods", "odt"}

DEPARTAMENTOS = ["Compras", "Manutenção", "Frota", "Oficina", "Diretoria / sócios", "Comercial / vendas",
                 "Financeiro", "Fiscal / NF", "Atendimento", "Outro"]
# departamento -> finalidade do canal (CanalEmpresa.Classificacao)
FINALIDADE_DEPTO = {"Compras": "COMPRAS", "Manutenção": "MANUTENCAO", "Frota": "MANUTENCAO", "Oficina": "MANUTENCAO",
                    "Comercial / vendas": "COMERCIAL", "Financeiro": "FINANCEIRO", "Fiscal / NF": "FISCAL",
                    "Atendimento": "ATENDIMENTO"}


def _telefone_ok(valor):
    dig = "".join(c for c in str(valor) if c.isdigit())
    if dig.startswith("55") and len(dig) in (12, 13):
        dig = dig[2:]
    return len(dig) in (10, 11) or (dig.startswith("0800") and len(dig) == 11)


class ContatoForm(forms.ModelForm):
    class Meta:
        model = Contato
        fields = ["nome", "cargo", "departamento", "telefone", "email", "observacoes"]
        widgets = {"observacoes": forms.Textarea(attrs={"rows": 2})}


class NaoContatarForm(forms.Form):
    motivo = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=True)


class GerarListaForm(forms.Form):
    nome = forms.CharField(max_length=200)
    limite = forms.IntegerField(required=False, min_value=1, help_text="Opcional: máximo de empresas")
