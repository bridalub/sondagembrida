"""
Reprocessa uma pesquisa com o que JÁ ESTÁ SALVO, sem nenhuma chamada paga (IA ou pesquisa web).

    python manage.py reprocessar_pesquisa 7            # simula: só mostra o que mudaria
    python manage.py reprocessar_pesquisa 7 --gravar

Usa apenas: respostas de pesquisa web já gravadas (Consultas web), leitura de sites (gratuita), base local da
Receita e a consulta cadastral complementar já integrada (BrasilAPI, gratuita, com limites e cache da fonte).
Passos:
  1. contatos mascarados (ex.: sa****@****.com) saem dos canais; o original fica no histórico da fonte;
  2. sites rejeitados só por nome diferente da razão social são reavaliados por evidências combinadas
     (telefone, endereço, município, nome citado com o mesmo CNPJ) – associa, ou mostra "Possível site";
  3. CNPJ pendente: confirma na base local / consulta complementar e procura a mesma raiz na região;
  4. refaz o cruzamento com a carteira local do Salesforce e as filas (conhecidas sempre separadas).
Nada é enviado e nada é gravado no Salesforce.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from carteira.models import CanalEmpresa, DadoColetado
from carteira.services.normalizacao import dominio_de_url
from prospeccao import conectores
from prospeccao.conectores.base import ContextoExecucao
from prospeccao.models import ConsultaWeb, Execucao, Fonte, Pesquisa, SelecaoPesquisa, linhas
from prospeccao.services import contatos as ct
from prospeccao.services import oportunidades as ops
from prospeccao.services import selecao as sl


class Simulacao(Exception):
    pass


class Command(BaseCommand):
    help = "Reprocessa uma pesquisa com dados já salvos (sem chamadas pagas)."

    def add_arguments(self, parser):
        parser.add_argument("pesquisa_id", type=int)
        parser.add_argument("--gravar", action="store_true")

    def handle(self, *args, **o):
        try:
            p = Pesquisa.objects.get(pk=o["pesquisa_id"])
        except Pesquisa.DoesNotExist:
            raise CommandError("Pesquisa não encontrada")
        self.w = self.stdout.write
        try:
            with transaction.atomic():
                self.executar(p)
                if not o["gravar"]:
                    raise Simulacao()
        except Simulacao:
            self.w(self.style.WARNING("SIMULAÇÃO: nada foi gravado (use --gravar)."))
            return
        self.w(self.style.SUCCESS("Gravado."))

    # ------------------------------------------------------------------
    def executar(self, p):
        from receita.importador import tabela
        from receita.models import CargaReceita
        from carteira.services.normalizacao import normalizar_texto
        ctx = ContextoExecucao()
        ex = Execucao.objects.filter(pesquisa=p).order_by("-pk").first()
        if ex is None:
            raise CommandError("Pesquisa sem execução")
        site_fonte = Fonte.objects.filter(codigo="site_publico", habilitada=True).first()
        leitor = conectores.IDENTIFICACAO["site_publico"](site_fonte) if site_fonte else None
        enr_fonte = Fonte.objects.filter(codigo="brasilapi_cnpj", habilitada=True).first()
        enr = conectores.ENRIQUECIMENTO["brasilapi_cnpj"](enr_fonte) if enr_fonte else None
        carga = CargaReceita.ativa_atual()
        cidades = {normalizar_texto(c) for c in linhas(p.cidades)}
        municipios = {c for c, d in tabela(carga.competencia, "MUNICIPIO").items() if normalizar_texto(d) in cidades}
        sits = {sl.SITS[s.strip().upper()] for s in (p.situacoes_receita or "ATIVA").split(",")
                if s.strip().upper() in sl.SITS}
        sels = list(SelecaoPesquisa.objects.filter(pesquisa=p).select_related("empresa", "frente"))
        empresas = {s.empresa for s in sels if s.empresa}

        # 1. mascarados
        n_masc = 0
        for e in empresas:
            for c in e.canais.filter(tipo__in=[CanalEmpresa.Tipo.EMAIL, CanalEmpresa.Tipo.TELEFONE]):
                if ct.mascarado(c.valor):
                    DadoColetado.objects.update_or_create(
                        empresa=e, campo=f"{c.tipo.lower()}_nao_disponibilizado:{c.fonte}"[:60], fonte=c.fonte[:60],
                        defaults={"valor": f"{c.valor} (ocultado pela fonte – não utilizável, não reconstruído)",
                                  "referencia": c.referencia[:500]})
                    self.w(f"  1. mascarado removido dos canais: {e.nome[:40]} {c.valor} ({c.fonte}) → histórico da fonte")
                    c.delete()
                    n_masc += 1

        # resultados de pesquisa já salvos (por empresa e por CNPJ citado)
        salvos = []
        for cw in ConsultaWeb.objects.filter(pesquisa=p, status="OK"):
            for r in cw.resultados if isinstance(cw.resultados, list) else []:
                if isinstance(r, dict) and r.get("url"):
                    salvos.append((cw, r))

        # 2. sites por evidências combinadas
        n_site = 0
        for s in sels:
            e = s.empresa
            if e is None or s.fila == SelecaoPesquisa.Fila.CONHECIDA or leitor is None:
                continue
            if e.canais.filter(tipo="SITE", confianca_associacao__in=["ALTA", "MEDIA"]).exists():
                continue
            pistas = [dict(r, dominio=r.get("dominio") or dominio_de_url(r["url"]))
                      for cw, r in salvos if cw.empresa_id == e.pk or
                      ct._digitos(r.get("cnpj_citado")) == e.cnpj]
            nomes = ct.nomes_citados_com_cnpj(e, pistas)
            cands, vistos = [], set()
            for r in pistas:
                if r.get("diretorio") or r.get("tipo") in ("DIRETORIO", "REDE_SOCIAL"):
                    continue
                d = r["dominio"]
                if d and d not in vistos:
                    vistos.add(d)
                    cands.append((r["url"], f"busca web salva ({r.get('verificacao', 'resultado')})"))
            if not cands:
                continue
            resultado = {"site": None, "paginas": [], "tentativas": [], "revisar": [], "leitura": {},
                         "aplica_a": "", "nomes_alt": nomes}
            antes = e.canais.count()
            ct._tentar_sites(e, leitor, ctx, cands[:3], resultado, resultado["leitura"])
            ct.registrar_pistas(e, pistas, dominio_de_url(resultado["site"] or ""), resultado["leitura"])
            if resultado["site"] or resultado["revisar"]:
                n_site += 1
            self.w(f"  2. {e.nome[:40]}: nomes citados com o CNPJ {nomes or '-'}; "
                   + "; ".join(f"{d}: {v[:150]}" for d, v in resultado["leitura"].items())
                   + f" | canais +{e.canais.count() - antes}")

        # 3. CNPJ pendente
        n_ident = 0
        for s in [s for s in sels if s.empresa is None and s.fila != SelecaoPesquisa.Fila.CONHECIDA]:
            self.w(f"  3. pendente {s.cnpj}: {sl.motivo_identidade_pendente(s)}")
            if enr is not None:
                try:
                    dados = enr.consultar(s.cnpj, ctx)
                    info = (f"consulta complementar (BrasilAPI): {dados.razao_social[:50]} – {dados.municipio}/{dados.uf}"
                            f" – situação {dados.situacao_cadastral}") if dados else "consulta complementar: CNPJ não encontrado"
                except Exception as exc:
                    info = f"consulta complementar falhou: {type(exc).__name__} – pendente"
                self.w(f"     {info}")
                s.fila_motivos = [sl.motivo_identidade_pendente(s), info]
                s.save(update_fields=["fila_motivos"])
            for irmao in sl.irmaos_na_regiao(carga, s.cnpj, municipios, sits):
                if SelecaoPesquisa.objects.filter(pesquisa=p, cnpj=irmao["cnpj"]).exists():
                    continue
                nova = SelecaoPesquisa.objects.create(
                    pesquisa=p, frente=s.frente, cnpj=irmao["cnpj"], plano=s.plano or p.plano_ativo,
                    motivos_selecao=list(s.motivos_selecao or [])[:1] + [
                        f"Possível correspondência: mesma raiz do CNPJ {s.cnpj} publicado na fonte (unidade fora da "
                        f"região); este estabelecimento fica na região – vínculo de grupo a conferir"],
                    primeira_execucao=ex, ultima_execucao=ex)
                empresa = sl._vincular(nova, ex, ctx)  # base local primeiro; consulta complementar só se faltar
                nova.empresa = empresa
                if empresa is not None:
                    ct.qualificar_cadastrais(empresa)
                    cfg = (p.plano_ativo.conteudo.get("por_frente", {}).get(str(nova.frente_id), {})
                           if p.plano_ativo else {})
                    nova.aderencia, nova.aderencia_motivos = sl.calcular_aderencia(nova, empresa, cfg, None)
                    ops.avaliar(empresa, p)
                nova.fila, nova.fila_motivos = sl.calcular_fila(nova, empresa, p)
                if empresa is not None:
                    from prospeccao.services import escopo as esc
                    perfil, mot_esc = esc.classificar(empresa)
                    if perfil == "FORA":
                        nova.aderencia, nova.aderencia_motivos = SelecaoPesquisa.Aderencia.FORA, [
                            f"Fora do escopo comercial: {mot_esc}"]
                        nova.fila, nova.fila_motivos = SelecaoPesquisa.Fila.FORA, nova.aderencia_motivos
                    elif perfil == "REVISAR":
                        nova.fila_motivos = list(nova.fila_motivos) + [f"Escopo a revisar: {mot_esc}"]
                nova.save()
                n_ident += 1
                self.w(f"     → mesma raiz na região: {irmao['cnpj']} {irmao['razao_social'][:50]} "
                       f"(CNAE {irmao['cnae_principal']}) → fila {nova.fila}: {'; '.join(nova.fila_motivos)[:150]}")

        # 4. cruzamento com a carteira local e filas
        for e in empresas:
            ops.avaliar(e, p)
        sl.recalcular_filas(p)
        self.w(f"Resumo: {n_masc} contato(s) mascarado(s) retirado(s); {n_site} site(s) reavaliado(s) com associação "
               f"ou 'possível site'; {n_ident} estabelecimento(s) da mesma raiz incluído(s) para conferência.")
        self.resumo(p)

    def resumo(self, p):
        from prospeccao.models import Oportunidade
        sels = SelecaoPesquisa.objects.filter(pesquisa=p).select_related("empresa")
        novas = [s for s in sels if s.empresa_id and Oportunidade.objects.filter(
            empresa_id=s.empresa_id, tipo="NOVA_EXTERNA").exists() and s.fila != "CONHECIDA"]
        vistos, grupos = set(), {"direto": [], "so_terceiro": [], "sem": []}
        for s in novas:
            if s.empresa_id in vistos:
                continue
            vistos.add(s.empresa_id)
            e = s.empresa
            if ct.canais_utilizaveis(e).exists():
                grupos["direto"].append(e.nome)
            elif ct.canais_terceiros(e).exists():
                grupos["so_terceiro"].append(e.nome)
            else:
                grupos["sem"].append(e.nome)
        pend = [s for s in sels if s.fila == "REVISAR_IDENTIDADE"]
        conhecidas = [s for s in sels if s.fila in ("CONHECIDA", "NAO_CONTATAR")]
        self.w(f"Leads novos (empresas distintas): {len(vistos)}")
        for k, v in grupos.items():
            self.w(f"  {k}: {len(v)} – {', '.join(n[:30] for n in v)}")
        self.w(f"Identidade pendente: {len(pend)} – {', '.join(s.cnpj for s in pend)}")
        self.w(f"Conhecidas no Salesforce (separadas): {len(conhecidas)}")
        for fila, n in sorted(__import__('collections').Counter(s.fila for s in sels).items()):
            self.w(f"  fila {fila}: {n}")
