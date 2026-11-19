"""
Reprocessa as RESPOSTAS DO GOOGLE já gravadas de uma pesquisa com as regras atuais – sem nenhuma chamada paga
(nem SerpApi nem IA). Usa a extração orgânica já gravada; lê sites indicados (gratuito); Receita local.

    python manage.py reprocessar_google 8            # simula
    python manage.py reprocessar_google 8 --gravar

1. aproveita TODOS os resultados de cada resposta (antes, a cota da frente descartava resultados já pagos);
2. região pela cidade/UF do endereço da fonte (marca 'fora da região' o que entrou por engano);
3. mesma empresa vinda de fontes diferentes: une registros criados em duplicidade e soma os contatos;
4. leitura gratuita dos sites indicados pelo Google para quem entrou agora;
5. refaz cruzamento com a carteira local e as filas. Nada vai ao Salesforce.
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from carteira.models import Empresa
from carteira.services.normalizacao import dominio_de_url, normalizar_texto
from prospeccao import conectores
from prospeccao.conectores.base import ContextoExecucao
from prospeccao.models import (Candidato, ConsultaWeb, Execucao, Fonte, Oportunidade, Pesquisa, SelecaoPesquisa,
                               linhas)
from prospeccao.services import contatos as ct
from prospeccao.services import descoberta_google as dg
from prospeccao.services import oportunidades as ops
from prospeccao.services import selecao as sl


class Simulacao(Exception):
    pass


class _Est:
    pass


class Command(BaseCommand):
    help = "Reprocessa respostas do Google já gravadas (sem chamadas pagas)."

    def add_arguments(self, parser):
        parser.add_argument("pesquisa_id", type=int)
        parser.add_argument("--gravar", action="store_true")

    def handle(self, *args, **o):
        self.w = self.stdout.write
        p = Pesquisa.objects.get(pk=o["pesquisa_id"])
        try:
            with transaction.atomic():
                self.executar(p)
                if not o["gravar"]:
                    raise Simulacao()
        except Simulacao:
            self.w(self.style.WARNING("SIMULAÇÃO: nada gravado (use --gravar)."))
            return
        self.w(self.style.SUCCESS("Gravado. Nenhuma chamada paga foi feita."))

    def executar(self, p):
        from receita.importador import tabela
        from receita.models import CargaReceita
        ctx = ContextoExecucao()
        ex = Execucao.objects.filter(pesquisa=p).order_by("-pk").first()
        if ex is None:  # execuções apagadas pela limpeza: registra esta como reprocessamento (sem chamadas pagas)
            from django.utils import timezone
            ex = Execucao.objects.create(pesquisa=p, gatilho=Execucao.Gatilho.COMANDO, status=Execucao.Status.CONCLUIDA,
                                         iniciada_em=timezone.now(), finalizada_em=timezone.now(), completa=True,
                                         mensagem="Reprocessamento das respostas do Google já gravadas – "
                                                  "nenhuma chamada paga (SerpApi ou IA)")
        plano = p.plano_ativo
        carga = CargaReceita.ativa_atual()
        cidades = linhas(p.cidades)
        municipios = {c for c, d in tabela(carga.competencia, "MUNICIPIO").items()
                      if normalizar_texto(d) in {normalizar_texto(x) for x in cidades}}
        sits = {sl.SITS[s.strip().upper()] for s in (p.situacoes_receita or "ATIVA").split(",") if s.strip().upper() in sl.SITS}
        est = _Est()
        est.vistos_google, est.consultas_norm, est.ids_execucao, est.externos = set(), set(), [], 0
        sf, leads = sl._conhecidos()
        por_nome = {f.nome: f for f in p.frentes.all()}
        antes_emp = set(SelecaoPesquisa.objects.filter(pesquisa=p, empresa__isnull=False).values_list("empresa_id", flat=True))

        n_reg = self.regiao(p, cidades)

        # 3. une duplicatas da mesma empresa (criadas automaticamente, sem ação humana)
        n_dup = 0
        sels = list(SelecaoPesquisa.objects.filter(pesquisa=p, empresa__isnull=False, empresa__cnpj__isnull=True)
                    .select_related("empresa").order_by("pk"))
        removidas = set()
        for i, a in enumerate(sels):
            for b in sels[i + 1:]:
                if b.empresa_id in removidas or a.empresa_id in removidas or a.empresa_id == b.empresa_id:
                    continue
                # telefone compartilhado não funde. Só domínio próprio ou o mesmo logradouro, com o mesmo nome.
                doms = lambda e: {dominio_de_url(v) for v in e.canais.filter(tipo="SITE").values_list("valor", flat=True)} - {""}  # noqa: E731
                mesmo_lugar = bool(a.empresa.endereco) and dg._rua(a.empresa.endereco) == dg._rua(b.empresa.endereco)
                sinal = bool(doms(a.empresa) & doms(b.empresa)) or mesmo_lugar
                if sinal and dg.mesma_empresa(a.empresa.nome, b.empresa.nome, cidades):
                    op_b = Oportunidade.objects.filter(empresa=b.empresa).first()
                    if b.empresa.abordagens.exists() or (op_b and (op_b.responsavel_id or op_b.status_definido_por_humano)):
                        continue  # houve ação humana: não une automaticamente
                    for c in b.empresa.canais.all():
                        ct.registrar(a.empresa, c.tipo, c.valor, fonte=c.fonte, url=c.referencia,
                                     origem=c.origem_canal, confianca=c.confianca_associacao, aplica_a=c.aplica_a,
                                     evidencia=c.evidencia_associacao)
                    Candidato.objects.filter(empresa=b.empresa).update(empresa=a.empresa)
                    from prospeccao.services.enriquecimento import registrar_dado
                    registrar_dado(a.empresa, f"unificado:{b.empresa.pk}", f"mesma empresa que “{b.empresa.nome}”",
                                   "reprocessamento", "")
                    self.w(f"  mesma empresa: “{b.empresa.nome}” unida a “{a.empresa.nome}”")
                    emp_b = b.empresa
                    SelecaoPesquisa.objects.filter(empresa=emp_b).delete()
                    Oportunidade.objects.filter(empresa=emp_b).delete()
                    emp_b.delete()
                    removidas.add(emp_b.pk)
                    n_dup += 1

        # 1. aproveita todos os resultados gravados
        cont = Counter()
        for cw in ConsultaWeb.objects.filter(pesquisa=p, provedor="google_serpapi", status="OK",
                                             finalidade__in=["DESCOBERTA", "COMPLEMENTAR"], empresa__isnull=True).order_by("pk"):
            fr = (cw.analise or {}).get("frente") or ""
            f = por_nome.get(fr) or next((x for n, x in por_nome.items() if n in str((cw.analise or {}).get("motivo"))), None)
            if f is None:
                continue
            itens = [dict(r) for r in cw.resultados or [] if isinstance(r, dict) and r.get("tipo") == "LOCAL"]
            ext = next((r["_extraidas"] for r in reversed(cw.resultados or []) if isinstance(r, dict) and "_extraidas" in r), [])
            from prospeccao.conectores.busca_web import eh_diretorio
            for e in ext:
                if e.get("tipo") == "LOCAL" or not e.get("nome_citado"):
                    continue
                dom = dominio_de_url(e.get("url", ""))
                itens.append({**e, "tipo": "ORGANICO", "dominio": dom, "diretorio": eh_diretorio(dom),
                              "modalidade": "organico (extração gravada)", "trecho": e.get("trecho", "")})
            for item in itens:
                st = dg.processar_item(item, cw.consulta, ex, ctx, est, f, plano, carga, municipios, sits, sf, leads,
                                       None, cidades[0] if cidades else "")
                cont[st.split("(")[0].strip()[:45]] += 1
        self.w("Respostas gravadas reaproveitadas: " + "; ".join(f"{v} × {k}" for k, v in cont.most_common()))

        # 4. leitura gratuita dos sites indicados para quem entrou agora + filas
        leitor = conectores.IDENTIFICACAO["site_publico"](Fonte.objects.get(codigo="site_publico"))
        novas = SelecaoPesquisa.objects.filter(pesquisa=p, empresa__isnull=False).exclude(empresa_id__in=antes_emp) \
            .select_related("empresa")
        for s in novas:
            r = dg.investigar_dirigido(s.empresa, s, p, None, leitor, None, ctx, max_buscas=0, sem_ia=True)
            if r.get("site"):
                self.w(f"  site lido: {s.empresa.nome[:40]} → {list(r['leitura'].values())[:1]}")
            ops.avaliar(s.empresa, p)
        for item in sl.reprocessar_respostas_salvas(p, ctx):
            if item["novos"] or item["acao"] in ("cnpj_vinculado", "conhecida_salesforce", "falha"):
                self.w(f"  salvas: {item['id']} {item['acao']} {item['motivo'][:120]}")
        # filas das que entraram agora (sem IA, sem busca): mesma regra de aderência e fila de uma execução real
        for s in SelecaoPesquisa.objects.filter(pesquisa=p, fila=SelecaoPesquisa.Fila.AGUARDANDO)                 .select_related("empresa"):
            cfg = (plano.conteudo.get("por_frente", {}).get(str(s.frente_id), {}) if plano else {})
            if s.empresa is not None and (s.aderencia != SelecaoPesquisa.Aderencia.FORA or not s.aderencia_motivos):
                s.aderencia, s.aderencia_motivos = sl.aderencia_com_google(s, s.empresa, cfg, None)
            s.fila, s.fila_motivos = sl.calcular_fila(s, s.empresa, p)
            s.ultima_execucao = ex
            s.save()
        n_reg += self.regiao(p, cidades)  # inclui as que entraram neste reprocessamento
        sl.recalcular_filas(p)
        self.w(f"Resumo: {n_reg} fora da região; {n_dup} duplicata(s) unida(s); {novas.count()} empresa(s) nova(s) "
               f"aproveitada(s) das respostas já pagas.")

    def regiao(self, p, cidades):
        """Região dos registros já criados, pela cidade/UF do endereço da fonte de origem."""
        n_reg = 0
        enderecos = {}
        for cw in ConsultaWeb.objects.filter(pesquisa=p, provedor="google_serpapi", status="OK"):
            for r in cw.resultados or []:
                if isinstance(r, dict) and r.get("endereco_citado"):
                    enderecos[normalizar_texto(r.get("nome_citado") or r.get("titulo"))] = r["endereco_citado"]
        for s in SelecaoPesquisa.objects.filter(pesquisa=p, empresa__isnull=False).select_related("empresa"):
            end = enderecos.get(normalizar_texto(s.empresa.nome), "")
            cid, uf = dg.cidade_uf_do_endereco(end)
            if cid and (cid not in {normalizar_texto(c) for c in cidades} or uf != p.uf.upper()):
                motivo = f"Fora da região: a fonte indica {cid.title()} - {uf} ({end[:70]})"
                s.fila, s.fila_motivos = SelecaoPesquisa.Fila.FORA, [motivo]
                s.aderencia, s.aderencia_motivos = SelecaoPesquisa.Aderencia.FORA, [motivo]
                s.save(update_fields=["fila", "fila_motivos", "aderencia", "aderencia_motivos"])
                Oportunidade.objects.filter(empresa=s.empresa).update(status="FORA_REGIAO", motivo_descarte=motivo[:255])
                n_reg += 1
                self.w(f"  região: {s.empresa.nome[:40]} → {motivo}")
        return n_reg
