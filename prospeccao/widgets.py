"""Lista com busca: campo "Buscar", caixas de seleção, Todos e Limpar (padrão da tela de configuração).

- Para ManyToMany / MultipleChoice: devolve a lista de valores marcados.
- Com texto=True: o campo do modelo é texto (um valor por linha); o widget junta os marcados com `sep`.
  Valores gravados que não estão na lista continuam aparecendo marcados (nada some ao editar).
- permitir_novo=True: Enter na busca acrescenta o que foi digitado (ex.: "ABC Paulista", prefixo de CNAE).
"""
from pathlib import Path

from django import forms
from django.conf import settings

from carteira.services.normalizacao import normalizar_texto


class ListaBusca(forms.Widget):
    template_name = "prospeccao/widgets/lista_busca.html"
    input_type = "lista"  # _campo.html: rótulo comum ligado ao botão, sem fieldset de checkboxes soltas
    allow_multiple_selected = True

    def __init__(self, attrs=None, choices=(), placeholder="Buscar", texto=False, sep="\n", permitir_novo=False,
                 vazio="Nenhum selecionado"):
        super().__init__(attrs)
        self.choices = list(choices)
        self.placeholder, self.texto, self.sep = placeholder, texto, sep
        self.permitir_novo, self.vazio = permitir_novo, vazio

    def __deepcopy__(self, memo):
        obj = super().__deepcopy__(memo)
        obj.choices = list(self.choices)
        return obj

    @staticmethod
    def _chave(v):
        return normalizar_texto(str(v))

    def format_value(self, value):
        if value is None or value == "":
            return []
        if isinstance(value, str):
            partes = value.replace(",", "\n").split("\n") if self.texto and self.sep == "," else value.split("\n")
            return [p.strip() for p in partes if p.strip()]
        return [str(v) for v in value if v not in (None, "")]

    def value_from_datadict(self, data, files, name):
        valores = data.getlist(name) if hasattr(data, "getlist") else data.get(name) or []
        if isinstance(valores, str):  # dados vindos como texto (um valor por linha), não como lista
            valores = [valores]
        valores = [v for v in valores if str(v).strip()]
        return self.sep.join(valores) if self.texto else valores

    def value_omitted_from_data(self, data, files, name):
        return False  # nada marcado é uma resposta (lista vazia), não campo ausente

    def get_context(self, name, value, attrs):
        ctx = super().get_context(name, value, attrs)
        marcados = self.format_value(value)
        sel = {self._chave(v) for v in marcados}
        opcoes, vistos = [], set()
        for v, rotulo in self.choices:
            v = str(getattr(v, "value", v))
            if v == "":
                continue
            k = self._chave(v)
            vistos.add(k)
            opcoes.append({"value": v, "label": str(rotulo), "checked": k in sel})
        extras = [{"value": v, "label": v, "checked": True} for v in marcados if self._chave(v) not in vistos]
        w = ctx["widget"]
        w.update(opcoes=extras + opcoes, placeholder=self.placeholder, permitir_novo=self.permitir_novo,
                 vazio=self.vazio, n_marcados=sum(1 for o in extras + opcoes if o["checked"]))
        return ctx

    def id_for_label(self, id_):
        return f"{id_}_botao" if id_ else id_


def municipios_atuacao():
    """Municípios de atuação (receita/municipios_atuacao.txt), com a grafia oficial para exibir e gravar."""
    arq = Path(settings.BASE_DIR) / "receita" / "municipios_atuacao.txt"
    try:
        nomes = [l.strip() for l in arq.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    except OSError:
        return []
    from prospeccao.services.prontidao import normalizar_municipios
    saida = []
    for nome in nomes:  # rótulo em maiúsculas, como na lista de atuação; valor com a grafia oficial quando conhecida
        oficiais, _corr, _desc = normalizar_municipios([nome])
        saida.append(((oficiais or [nome])[0], normalizar_texto(nome)))
    return sorted(saida, key=lambda x: x[1])


def cnaes_oficiais():
    """Tabela oficial de CNAE da Receita (código – descrição). Vazia se nenhuma tabela foi baixada ainda."""
    from receita.importador import tabela
    from receita.models import CargaReceita
    competencias = list(CargaReceita.objects.exclude(competencia="").order_by("-competencia")
                        .values_list("competencia", flat=True).distinct()[:3])
    for comp in competencias:
        try:
            t = tabela(comp, "CNAE")
        except Exception:
            continue
        if t:
            return [(c, f"{c[:4]}-{c[4:5]}/{c[5:]} – {d}" if len(c) == 7 else f"{c} – {d}") for c, d in sorted(t.items())]
    return []
