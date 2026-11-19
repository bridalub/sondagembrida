"""
- Fontes novas: base Receita (descoberta com CNPJ) e IA (opcional, desligada sem credencial).
- Correção: "área industrial"/"edifício industrial" são LOCALIZAÇÃO, não comprovam máquinas nem atividade.
  Os padrões saem da regra e as evidências geradas só por eles são desativadas (não apagadas).
- Canais vindos da Receita (BrasilAPI) passam a ser CADASTRAIS.
"""
import re
import unicodedata

from django.db import migrations

REMOVER = {"landuse industrial", "building industrial", "landuse farmyard", "landuse construction"}


def _norm(t):
    t = "".join(c for c in unicodedata.normalize("NFKD", t or "") if not unicodedata.combining(c)).upper()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9]", " ", t)).strip()


def aplicar(apps, schema_editor):
    Fonte = apps.get_model("prospeccao", "Fonte")
    Regra = apps.get_model("prospeccao", "RegraEvidencia")
    Evidencia = apps.get_model("prospeccao", "Evidencia")
    Canal = apps.get_model("carteira", "CanalEmpresa")
    Fonte.objects.update_or_create(codigo="receita_cnpj", defaults=dict(
        nome="Receita Federal – dados abertos do CNPJ (base local)", tipo="DESCOBERTA", intervalo_minimo_segundos=0,
        max_consultas_execucao=1000, timeout_segundos=30, max_retentativas=0,
        licenca="Dados públicos (Receita Federal, dados abertos)",
        termos_uso_url="https://arquivos.receitafederal.gov.br",
        dados_permitidos="Estabelecimentos e Empresas do CNPJ: razão social, fantasia, matriz/filial, situação, "
                         "abertura, CNAEs, endereço, telefones e e-mail CADASTRAIS. Sócios e Simples não são importados.",
        observacoes="Consulta a base local importada (manage.py importar_receita). Competência mensal: não é "
                    "consulta em tempo real."))
    Fonte.objects.update_or_create(codigo="ia_anthropic", defaults=dict(
        nome="IA – Anthropic Claude (opcional)", tipo="ANALISE", habilitada=True, intervalo_minimo_segundos=0,
        max_consultas_execucao=30, max_consultas_dia=300, timeout_segundos=90, max_retentativas=2,
        variavel_credencial="ANTHROPIC_API_KEY",
        dados_permitidos="Envia só nome/CNAE públicos e trechos coletados do site da própria empresa. Nada da carteira.",
        observacoes="Ativa só com PROSPECCAO_IA_PROVEDOR=anthropic e ANTHROPIC_API_KEY. Resultados = hipóteses."))
    for r in Regra.objects.filter(alvo="ATIVIDADE_FONTE"):
        novos = [p for p in r.padroes.replace("\r", "").split("\n") if p.strip() and p.strip().lower() not in REMOVER]
        if len(novos) != len([p for p in r.padroes.split("\n") if p.strip()]):
            r.padroes = "\n".join(novos)
            r.save(update_fields=["padroes"])
            pads = [_norm(p) for p in novos]
            for ev in Evidencia.objects.filter(regra=r, ativa=True):
                texto = _norm(ev.trecho)
                if not any(re.search(r"(?<![A-Z0-9])" + re.escape(p) + r"(?![A-Z0-9])", texto) for p in pads if p):
                    ev.ativa = False
                    ev.save(update_fields=["ativa"])
    Canal.objects.filter(fonte="brasilapi_cnpj").update(verificacao="CADASTRAL")


class Migration(migrations.Migration):
    dependencies = [("prospeccao", "0003_execucao_complementados_execucao_ia_analises_and_more"),
                    ("carteira", "0003_canalempresa_verificacao_and_more")]
    operations = [migrations.RunPython(aplicar, migrations.RunPython.noop)]
