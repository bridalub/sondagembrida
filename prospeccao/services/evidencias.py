"""
Regras transparentes e editáveis (RegraEvidencia) aplicadas a textos das fontes.

- Texto do site / categoria da fonte  -> CONFIRMADO_FONTE (a fonte informa a operação/atividade)
- Nome da empresa / CNAE              -> HIPOTESE (indício; precisa confirmação)
- A APLICAÇÃO dos produtos é sempre "possível": nunca afirmamos consumo, volume ou especificação.
O conteúdo externo é tratado só como texto para casamento de padrões; nunca como instrução.
"""
import hashlib
import re
import unicodedata

from prospeccao.models import Evidencia, RegraEvidencia, linhas

CLASSIFICACAO_POR_ALVO = {
    RegraEvidencia.Alvo.TEXTO_SITE: Evidencia.Classificacao.CONFIRMADO_FONTE,
    RegraEvidencia.Alvo.ATIVIDADE_FONTE: Evidencia.Classificacao.CONFIRMADO_FONTE,
    RegraEvidencia.Alvo.NOME: Evidencia.Classificacao.HIPOTESE,
    RegraEvidencia.Alvo.CNAE: Evidencia.Classificacao.HIPOTESE,
    RegraEvidencia.Alvo.CNAE_DESCRICAO: Evidencia.Classificacao.HIPOTESE,
}


def normalizar_mesmo_tamanho(texto):
    """Maiúsculas, sem acento, não alfanumérico -> espaço; mantém o comprimento (para recortar o trecho original)."""
    saida = []
    for ch in texto or "":
        base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))[:1] or " "
        saida.append(base.upper() if base.isalnum() else " ")
    return "".join(saida)


def _padrao(p):
    p = normalizar_mesmo_tamanho(p).strip()
    return re.compile(r"(?<![A-Z0-9])" + r"\s+".join(re.escape(x) for x in p.split()) + r"(?![A-Z0-9])") if p else None


# Contextos em que a menção NÃO comprova operação própria (financiar, segurar, vender para, atender clientes...)
NEGATIVOS_PADRAO = [
    "financiamento", "financiar", "financiamos", "credito", "consorcio", "seguro", "seguros", "seguradora", "cotacao",
    "para sua frota", "para frotas", "para transportadoras", "para empresas de", "atendemos", "nossos clientes",
    "clientes como", "representante", "revendedor", "distribuidor autorizado", "vendemos", "noticia", "blog",
    "credito rural", "seguro agricola", "linha de credito", "linhas de credito", "emprestimo",
]
_NEG_CACHE = {}


def contexto_negativo(norm_texto, ini, fim, negativos=None, janela=120):
    """Retorna o termo negativo encontrado perto da menção (ou '')."""
    chave = tuple(negativos or NEGATIVOS_PADRAO)
    if chave not in _NEG_CACHE:
        _NEG_CACHE[chave] = [x for x in (_padrao(n) for n in chave) if x]
    trecho = norm_texto[max(0, ini - janela):fim + janela]
    for p, n in zip(_NEG_CACHE[chave], chave):
        if p.search(trecho):
            return n
    return ""


def _trecho(texto, ini, fim, margem=110):
    a, b = max(0, ini - margem), min(len(texto), fim + margem)
    t = re.sub(r"\s+", " ", texto[a:b]).strip()
    return (("…" if a > 0 else "") + t + ("…" if b < len(texto) else ""))[:300]


def _chave(empresa, candidato, regra, fonte, url):
    # estável: não muda quando o candidato é vinculado depois a uma empresa
    dono = f"c{candidato.pk}" if candidato else f"e{empresa.pk}"
    base = f"{dono}|{regra.pk}|{fonte}|{url}"
    return hashlib.sha256(base.encode()).hexdigest()


def _criar(regra, *, empresa, candidato, fonte, url, trecho, descricao_extra=""):
    chave = _chave(empresa, candidato, regra, fonte, url)
    ev, criada = Evidencia.objects.get_or_create(chave=chave, defaults=dict(
        empresa=empresa, candidato=candidato, tipo=regra.tipo_evidencia,
        descricao=(regra.descricao + descricao_extra)[:255], classificacao=CLASSIFICACAO_POR_ALVO[regra.alvo],
        fonte=fonte, url=url[:500], trecho=trecho[:300], regra=regra, gerada_por="REGRA"))
    if criada:
        ev.aplicacoes.set(regra.aplicacoes.all())
    elif not ev.ativa or (empresa and ev.empresa_id != empresa.pk):
        ev.ativa = True
        ev.empresa = empresa or ev.empresa
        ev.save(update_fields=["ativa", "empresa"])
    return ev, criada


def aplicar_textos(alvo, textos, *, empresa=None, candidato=None, negativos=None):
    """
    textos: lista de (texto, url, fonte). Uma evidência por regra/fonte/url (primeiro trecho encontrado).
    Em texto de site, menção em contexto negativo (financiamento, venda, clientes...) vira INSUFICIENTE.
    """
    novas = 0
    for regra in RegraEvidencia.objects.filter(ativa=True, alvo=alvo).prefetch_related("aplicacoes"):
        padroes = [p for p in (_padrao(x) for x in linhas(regra.padroes)) if p]
        for texto, url, fonte in textos:
            if not texto:
                continue
            norm = normalizar_mesmo_tamanho(texto)
            achou = None
            for p in padroes:
                for m in p.finditer(norm):
                    neg = contexto_negativo(norm, m.start(), m.end(), negativos) if alvo == "TEXTO_SITE" else ""
                    if not neg:
                        achou = (m, "")
                        break
                    achou = achou or (m, neg)
                if achou and not achou[1]:
                    break
            if achou:
                m, neg = achou
                ev, criada = _criar(regra, empresa=empresa, candidato=candidato, fonte=fonte, url=url,
                                    trecho=_trecho(texto, m.start(), m.end()))
                if neg and ev.classificacao != Evidencia.Classificacao.INSUFICIENTE:
                    ev.classificacao = Evidencia.Classificacao.INSUFICIENTE
                    ev.descricao = f"Menção em contexto de '{neg}' (não comprova operação própria): {regra.descricao}"[:255]
                    ev.save(update_fields=["classificacao", "descricao"])
                novas += criada
    return novas


def aplicar_cnaes(cnaes, *, empresa, fonte, url=""):
    """cnaes: lista de (codigo, descricao). Regras CNAE (prefixo) e CNAE_DESCRICAO."""
    novas = 0
    cnaes = [(re.sub(r"\D", "", str(c)), d or "") for c, d in cnaes if c]
    for regra in RegraEvidencia.objects.filter(ativa=True, alvo__in=[RegraEvidencia.Alvo.CNAE,
                                                                     RegraEvidencia.Alvo.CNAE_DESCRICAO]):
        for codigo, desc in cnaes:
            if regra.alvo == RegraEvidencia.Alvo.CNAE:
                ok = any(codigo.startswith(re.sub(r"\D", "", p)) for p in linhas(regra.padroes) if re.sub(r"\D", "", p))
            else:
                norm = normalizar_mesmo_tamanho(desc)
                ok = any(p and p.search(norm) for p in (_padrao(x) for x in linhas(regra.padroes)))
            if ok:
                _, criada = _criar(regra, empresa=empresa, candidato=None, fonte=fonte, url=url,
                                   trecho=f"CNAE {codigo} – {desc}"[:300])
                novas += criada
                break
    return novas


def garantir_insuficiente(empresa, fontes_consultadas):
    """Sem evidência real: registra 'informação insuficiente' (visível), nunca 'sem potencial'."""
    chave = hashlib.sha256(f"insuficiente|{empresa.pk}".encode()).hexdigest()
    reais = Evidencia.objects.filter(empresa=empresa, ativa=True).exclude(classificacao=Evidencia.Classificacao.INSUFICIENTE)
    if reais.exists():
        Evidencia.objects.filter(chave=chave).update(ativa=False)
        return
    Evidencia.objects.update_or_create(chave=chave, defaults=dict(
        empresa=empresa, tipo="ATIVIDADE", classificacao=Evidencia.Classificacao.INSUFICIENTE, ativa=True,
        descricao="Nenhuma evidência de operação encontrada nas fontes consultadas – aplicação a investigar",
        fonte=", ".join(sorted(fontes_consultadas))[:60] or "—", gerada_por="REGRA"))
