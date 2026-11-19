"""
Piloto CONTROLADO da IA + pesquisa web, em ETAPAS, cada uma com teto próprio e critério de parada.

    python manage.py piloto_ia --pesquisa 3                                    # SIMULAÇÃO: amostra sugerida e
                                                                               # custo de cada etapa (nada pago)
    python manage.py piloto_ia --pesquisa 3 --etapa plano --teto-usd 0.01      # A. plano por IA (revisar consultas)
    python manage.py piloto_ia --pesquisa 3 --etapa controle --ids 32 --teto-usd 0.06
                                                                               # B. empresa de CONTROLE (já conhecida)
    python manage.py piloto_ia --pesquisa 3 --etapa empresas --ids 2,6,10,29 --teto-usd 0.25
                                                                               # C. empresas sem contato utilizável
    python manage.py piloto_ia --pesquisa 3 --etapa descoberta --frente 1 --teto-usd 0.06
                                                                               # D. 1 consulta de descoberta, 1 frente
    python manage.py piloto_ia --relatorio <execução> [--csv tabela.csv] [--planilha revisao.csv]
    python manage.py piloto_ia --importar-revisao revisao.csv                  # marcações CERTO / ERRADO / NAO_SEI

--teto-usd é o teto DESTA etapa (somado ao já gasto na pesquisa). A reserva é estimativa conservadora: os tokens de
entrada da pesquisa web não têm limite técnico, então a chamada em andamento pode excedê-la.
Controle: o caminho gratuito (site conhecido, domínio do e-mail) é IGNORADO, e a pesquisa precisa reencontrar o que
já se sabe – mede acerto da busca e da associação. Somente leitura: nada é enviado e nada vai ao Salesforce.
Empresas conhecidas no Salesforce (clientes, leads, bloqueadas) não entram. Sem ampliação automática.
"""
import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum
from django.utils import timezone

from carteira.models import CanalEmpresa
from carteira.services.normalizacao import dominio_de_url
from prospeccao.models import ConsultaWeb, Execucao, Pesquisa, SelecaoPesquisa, UsoIA

MAX_EMPRESAS = 5
PRIORIDADE = [SelecaoPesquisa.Fila.BUSCAR_CONTATO, SelecaoPesquisa.Fila.CONTATO_CADASTRAL]
REVISAO = {"CERTO": CanalEmpresa.Confirmacao.CONFERIDO, "ERRADO": CanalEmpresa.Confirmacao.INCORRETO}


def _canais(empresa):
    return [{"id": c.pk, "tipo": c.tipo, "valor": c.valor, "nivel": c.nivel, "confianca": c.confianca_associacao,
             "aplica_a": c.aplica_a, "fonte": c.fonte, "ref": c.referencia, "finalidade": c.classificacao,
             "confirmacao": c.confirmacao_humana} for c in empresa.canais.all()] if empresa else []


def _desc(s):
    return ", ".join(f"{c['tipo'][:3]} {c['valor']} ({c['nivel']})" for c in _canais(s.empresa)) or "nenhum"


def sugerir(pesquisa):
    """Controle (site e contato já conhecidos) + empresas NO ESCOPO sem contato utilizável, priorizando pertinência
    (perfil central da frente e aderência) e, depois, casos difíceis de contato. Porte só ordena, não exclui."""
    from prospeccao.services import escopo as esc
    base = SelecaoPesquisa.objects.filter(pesquisa=pesquisa, empresa__isnull=False) \
        .exclude(decisao_humana="RETIRAR").select_related("empresa", "frente")
    controle = None
    for s in base.filter(fila=SelecaoPesquisa.Fila.SELECIONADA).order_by("-pontuacao"):
        e = s.empresa
        if e.canais.filter(tipo="SITE", confianca_associacao__in=["ALTA", "MEDIA"]).exists() and \
                e.canais.filter(tipo__in=["TELEFONE", "EMAIL"], origem_canal="PUBLICACAO").exists():
            controle = s
            break
    cands = [s for s in base.filter(fila__in=PRIORIDADE) if esc.pertinente_para_gasto(s.empresa)[0]]
    tels = {}
    for s in cands:
        for c in s.empresa.canais.filter(tipo="TELEFONE"):
            tels.setdefault(c.valor, set()).add(s.cnpj)

    def motivos(s):
        e, m = s.empresa, []
        if e.canais.filter(tipo="EMAIL").filter(valor__iregex=r"contab|contador|fiscal").exists():
            m.append("e-mail cadastral de contador/fiscal")
        if any(len(tels[c.valor]) > 1 for c in e.canais.filter(tipo="TELEFONE")):
            m.append("telefone cadastral repetido em outra empresa")
        if e.cnpj[8:12] != "0001":
            m.append("filial (testa matriz x filial)")
        return m
    ader = {"SUSTENTADA": 0, "PROVAVEL": 1, "INSUFICIENTE": 2, "FORA": 3}
    porte = {"DEMAIS": 0, "EMPRESA DE PEQUENO PORTE": 1, "MICRO EMPRESA": 2}
    cands.sort(key=lambda s: (ader.get(s.aderencia_efetiva, 9), PRIORIDADE.index(s.fila),
                              porte.get(s.empresa.porte, 3), -len(motivos(s)), -s.pontuacao))
    vistos, casos = set(), []
    for s in cands:
        if s.cnpj not in vistos:
            vistos.add(s.cnpj)
            casos.append((s, [f"{esc.PERFIS[esc.escopo_efetivo(s.empresa)[0]]}, aderência {s.aderencia_efetiva.lower()}"]
                          + (motivos(s) or ["sem contato utilizável" if s.fila == "BUSCAR_CONTATO"
                                            else "só contato cadastral"])))
    return controle, casos[:MAX_EMPRESAS - 1]


class Command(BaseCommand):
    help = "Piloto controlado em etapas (plano, controle, empresas, descoberta), com teto por etapa e revisão."

    def add_arguments(self, parser):
        parser.add_argument("--pesquisa", type=int)
        parser.add_argument("--etapa", choices=["plano", "controle", "empresas", "descoberta"])
        parser.add_argument("--ids", help="Ids das seleções (separados por vírgula)")
        parser.add_argument("--frente", type=int, help="Frente da descoberta (etapa D)")
        parser.add_argument("--teto-usd", help="Teto DESTA etapa, ex.: 0.06")
        parser.add_argument("--relatorio", type=int, help="Id da execução de uma etapa")
        parser.add_argument("--csv", help="Grava a tabela da amostra em CSV")
        parser.add_argument("--planilha", help="Grava a planilha de revisão (sites/contatos encontrados)")
        parser.add_argument("--importar-revisao", help="Lê a planilha revisada (CERTO / ERRADO / NAO_SEI)")

    # ------------------------------------------------------------------ entrada
    def handle(self, *args, **o):
        if o["importar_revisao"]:
            return self.importar_revisao(o["importar_revisao"])
        if o["relatorio"]:
            return self.relatorio(o["relatorio"], o["csv"], o["planilha"])
        if not o["pesquisa"]:
            raise CommandError("Informe --pesquisa")
        try:
            p = Pesquisa.objects.get(pk=o["pesquisa"], modo="PLANEJADA")
        except Pesquisa.DoesNotExist:
            raise CommandError("Pesquisa planejada não encontrada")
        if not o["etapa"]:
            return self.simular(p)
        return self.executar_etapa(p, o)

    def simular(self, p):
        from prospeccao.services import ia, orcamento as orcm
        w = self.stdout.write
        controle, casos = sugerir(p)
        w(f"Pesquisa {p.pk}: {p.nome}")
        if controle:
            w(f"Controle sugerido: [{controle.pk}] {controle.empresa.nome} | {controle.empresa.cnpj_formatado} | "
              f"já conhecido: {_desc(controle)}")
        else:
            w(self.style.WARNING("Nenhuma empresa externa com site e contato já conhecidos para servir de controle"))
        w("Empresas sugeridas (no escopo, priorizando pertinência; depois contato a resolver e casos difíceis):")
        for s, mot in casos:
            w(f"  [{s.pk}] {s.empresa.nome[:60]} | {s.empresa.cnpj_formatado} | {s.frente.nome if s.frente else '-'} | "
              f"{'; '.join(mot)} | hoje: {_desc(s)}")
        m = ia.modelo_configurado()
        etapas = [("A. plano", orcm.estimativa(p, 0, modelo=m, descoberta_por_frente=0)),
                  ("B. controle (1)", orcm.estimativa(p, 1, modelo=m, descoberta_por_frente=0, com_plano=False)),
                  (f"C. empresas ({len(casos)})", orcm.estimativa(p, len(casos), modelo=m, descoberta_por_frente=0,
                                                                  com_plano=False)),
                  ("D. descoberta (1 consulta, 1 frente)", orcm.estimativa(p, 0, com_ia=False, modelo=m,
                                                                           descoberta_por_frente=1, n_frentes=1))]
        w("Custo por etapa (típico | soma das reservas; a reserva é estimativa conservadora):")
        tt = tm = 0
        for nome, e in etapas:
            tt, tm = tt + e["total_tipico_usd"], tm + e["total_max_usd"]
            w(f"  {nome}: US$ {e['total_tipico_usd']:.4f} | US$ {e['total_max_usd']:.4f} "
              f"({e['chamadas_ia']} IA, {e['chamadas_busca']} pesquisa(s), até {e['acoes_pesquisa_max']} ações)")
        w(f"  Total: US$ {tt:.4f} | US$ {tm:.4f}")
        w(f"Gasto já registrado: pesquisa US$ {orcm.gasto_registrado(p):.4f}; aplicação US$ "
          f"{orcm.gasto_registrado():.4f}; teto global US$ {orcm.teto_global()}")
        w("Critérios de parada: A – consultas sem sentido comercial; B – controle não reencontrado ou associado errado; "
          "C – associação errada ou custo muito acima do previsto; D – só diretórios/concorrentes.")
        w(self.style.WARNING("SIMULAÇÃO: nenhuma chamada paga foi feita."))

    # ------------------------------------------------------------------ execução de uma etapa
    def executar_etapa(self, p, o):
        from prospeccao.conectores.busca_web import obter_busca
        from prospeccao.services import ia, orcamento as orcm
        w = self.stdout.write
        if not o["teto_usd"]:
            raise CommandError("Cada etapa exige --teto-usd. Nenhuma chamada foi feita.")
        try:
            teto = Decimal(o["teto_usd"])
        except InvalidOperation:
            raise CommandError("--teto-usd inválido")
        tg = orcm.teto_global()
        if tg is None:
            raise CommandError("Defina PROSPECCAO_TETO_GLOBAL_USD no .env. Nenhuma chamada foi feita.")
        ok_ia, msg_ia = ia.status()
        busca, msg_busca = obter_busca()
        if not ok_ia or (o["etapa"] != "plano" and busca is None):
            raise CommandError(f"Integrações não prontas ({msg_ia}; {msg_busca}). Nenhuma chamada foi feita.")
        ids = [int(x) for x in (o["ids"] or "").split(",") if x.strip()]
        sels = list(SelecaoPesquisa.objects.filter(pesquisa=p, pk__in=ids).select_related("empresa", "frente"))
        if o["etapa"] in ("controle", "empresas"):
            if not sels or len(sels) != len(ids):
                raise CommandError("Informe --ids válidos desta pesquisa")
            if len(sels) > MAX_EMPRESAS or (o["etapa"] == "controle" and len(sels) != 1):
                raise CommandError("Controle: 1 empresa; empresas: até 4 (sem ampliação automática)")
            if any(s.fila == SelecaoPesquisa.Fila.CONHECIDA for s in sels):
                raise CommandError("Empresas conhecidas no Salesforce não entram no piloto")
        if o["etapa"] == "descoberta" and not p.frentes.filter(pk=o["frente"] or 0).exists():
            raise CommandError("Informe --frente (id de uma frente desta pesquisa)")
        gasto_p = orcm.gasto_registrado(p)
        antigo = p.teto_custo_usd
        p.teto_custo_usd = gasto_p + teto  # teto DESTA etapa sobre o acumulado da pesquisa
        p.save(update_fields=["teto_custo_usd"])
        w(f"Etapa {o['etapa']}: teto da etapa US$ {teto} (teto acumulado da pesquisa {antigo} -> {p.teto_custo_usd}; "
          f"teto global US$ {tg}, já registrado na aplicação US$ {orcm.gasto_registrado():.4f})")
        w("A reserva é estimativa conservadora: os tokens de entrada da pesquisa não têm limite técnico.")
        inicio = timezone.now()
        if o["etapa"] == "plano":
            return self.etapa_plano(p, inicio)
        antes = {str(s.pk): {"fila": s.fila, "canais": _canais(s.empresa)} for s in sels}
        params = {"somente_investigar": True, "piloto": True, "etapa": o["etapa"], "antes": antes,
                  "inicio": inicio.isoformat(), "teto_usd": str(teto), "investigar_ids": [s.pk for s in sels]}
        if o["etapa"] == "controle":
            params["controle_ids"] = [s.pk for s in sels]
            params["controle_esperado"] = {
                str(s.pk): {"sites": sorted({dominio_de_url(c.valor) for c in s.empresa.canais.filter(
                    tipo="SITE", confianca_associacao__in=["ALTA", "MEDIA"])}),
                    "contatos": sorted(s.empresa.canais.filter(tipo__in=["TELEFONE", "EMAIL"], origem_canal="PUBLICACAO")
                                       .values_list("valor", flat=True))} for s in sels}
        if o["etapa"] == "descoberta":
            params.update(investigar_ids=[-1], descoberta_por_frente=1, frentes_ids=[o["frente"]])
        from prospeccao.services import fila
        from prospeccao.services.pipeline import executar
        ex, nova = fila.enfileirar(p, Execucao.Gatilho.COMANDO, parametros=params)
        if not nova:
            raise CommandError(f"Já existe execução ativa ({ex.pk}) para esta pesquisa")
        w(f"Execução {ex.pk} iniciada...")
        executar(ex.pk)
        self.relatorio(ex.pk, o["csv"], o["planilha"])

    def etapa_plano(self, p, inicio):
        from prospeccao.services import orcamento as orcm, planejamento
        w = self.stdout.write
        try:
            plano = planejamento.plano_ia(p)
        except Exception as e:
            raise CommandError(f"Plano por IA não gerado ({type(e).__name__}: {str(e)[:300]}). O plano anterior "
                               f"continua ativo e não é apresentado como IA.")
        c = plano.conteudo
        w(self.style.SUCCESS(f"Plano por IA: versão {plano.versao} ({plano.observacao}). Revise antes da etapa D."))
        w(f"Interpretação: {c.get('interpretacao', '')[:500]}")
        w(f"Justificativa: {c.get('justificativa_ia', '')[:500]}")
        w(f"Consultas por empresa: {c.get('consultas_externas')}")
        for fid, cfg in (c.get("por_frente") or {}).items():
            w(f"  Frente {fid}: termos {cfg.get('termos', [])[:8]} | consultas de descoberta "
              f"{cfg.get('consultas_descoberta', [])}")
        if c.get("ambiguidades"):
            w(f"Ambiguidades: {c['ambiguidades']}")
        if c.get("exclusoes_sugeridas_pela_ia"):
            w(f"Exclusões SUGERIDAS (não aplicadas): {c['exclusoes_sugeridas_pela_ia']}")
        u = UsoIA.objects.filter(pesquisa=p, tipo="PLANO", criado_em__gte=inicio).order_by("-criado_em").first()
        if u:
            w(f"Consumo: {u.tokens_entrada}+{u.tokens_saida} tokens, {u.duracao_ms} ms, US$ {u.custo_estimado_usd} "
              f"(calculado pelo uso retornado; cobrança a confirmar no painel)")
        w(f"Gasto registrado na aplicação: US$ {orcm.gasto_registrado():.5f}")

    # ------------------------------------------------------------------ relatório
    def relatorio(self, ex_id, caminho_csv=None, caminho_planilha=None):
        from prospeccao.services import contatos as ct, orcamento as orcm
        try:
            ex = Execucao.objects.select_related("pesquisa").get(pk=ex_id)
        except Execucao.DoesNotExist:
            raise CommandError("Execução não encontrada")
        par = ex.parametros or {}
        if not par.get("piloto"):
            raise CommandError("Esta execução não é do piloto")
        w = self.stdout.write
        p = ex.pesquisa
        inicio = datetime.fromisoformat(par["inicio"])
        w("")
        w(f"=== Piloto – etapa {par.get('etapa', '-')} – execução {ex.pk} ({ex.get_status_display()}) ===")
        for lim in ex.limites_atingidos or []:
            w(self.style.WARNING(f"  limite/pendência: {lim}"))
        if ex.mensagem:
            w(self.style.WARNING(f"  {ex.mensagem}"))
        linhas, planilha = [], []
        tot = {"sites": 0, "novos": 0, "corroborados": 0, "incorretos": 0, "abordagem": 0}
        for sid, a in par.get("antes", {}).items():
            s = SelecaoPesquisa.objects.select_related("empresa", "frente").filter(pk=int(sid)).first()
            if not s or not s.empresa:
                continue
            e = s.empresa
            ids_antes = {c["id"] for c in a["canais"]}
            canais = list(e.canais.all())
            novos = [c for c in canais if c.pk not in ids_antes]
            tot["sites"] += len([c for c in novos if c.tipo == CanalEmpresa.Tipo.SITE])
            tot["novos"] += len([c for c in novos if c.tipo != CanalEmpresa.Tipo.SITE])
            tot["corroborados"] += len([c for c in canais if c.nivel == "CORROBORADO"])
            tot["incorretos"] += len([c for c in canais if c.nivel == "INCORRETO"])
            uteis = list(ct.canais_utilizaveis(e))
            tot["abordagem"] += bool(uteis)
            ordem = ["CONFIRMADO_EQUIPE", "CONFERIDO_EQUIPE", "INFORMADO_EQUIPE", "CORROBORADO", "PUBLICADO",
                     "CADASTRAL", "PISTA"]

            def melhor(tipo):
                cs = sorted((c for c in canais if c.tipo == tipo and c.nivel != "INCORRETO"),
                            key=lambda c: ordem.index(c.nivel))
                return cs[0] if cs else None
            tel, mail = melhor(CanalEmpresa.Tipo.TELEFONE), melhor(CanalEmpresa.Tipo.EMAIL)
            site = next((c for c in canais if c.tipo == CanalEmpresa.Tipo.SITE
                         and c.confianca_associacao in ("ALTA", "MEDIA")), None)
            pend = list((s.fila_motivos or [])[:2])
            if not uteis:
                pend.append("sem canal utilizável")
            pend += [f"pista a conferir: {c.valor}" for c in canais if c.nivel == "PISTA" and c.tipo != "SITE"][:2]
            linhas.append({
                "Empresa": e.nome, "CNPJ": e.cnpj_formatado, "Frente": s.frente.nome if s.frente else "-",
                "Motivo da seleção": "; ".join(str(m) for m in (s.motivos_selecao or [])[:2]),
                "Telefone": f"{tel.valor} [{tel.get_aplica_a_display()}; {tel.nivel_display}]" if tel else "-",
                "E-mail": f"{mail.valor} [{mail.get_aplica_a_display()}; {mail.nivel_display}]" if mail else "-",
                "Fonte": ", ".join(sorted({c.fonte for c in (tel, mail) if c})) or "-",
                "Qualidade da associação": (f"site {site.confianca_associacao}: {site.evidencia_associacao[:90]}"
                                            if site else "site não associado"),
                "Pendências": "; ".join(pend)[:300] or "-",
                "Antes": f"fila {a['fila']}, {len(a['canais'])} canal(is)",
                "Depois": f"fila {s.fila}, {len(canais)} canal(is), {len(novos)} novo(s)"})
            for c in canais:
                if c.pk not in ids_antes or c.coletado_em >= inicio:
                    planilha.append({
                        "canal_id": c.pk, "empresa": e.nome, "cnpj": e.cnpj_formatado, "tipo": c.tipo, "valor": c.valor,
                        "nivel": c.nivel_display, "aplica_a": c.get_aplica_a_display(),
                        "finalidade": c.get_classificacao_display(), "confianca": c.confianca_associacao,
                        "fonte": c.fonte, "url": c.referencia, "evidencia": c.evidencia_associacao[:300],
                        "novo": "sim" if c.pk not in ids_antes else "não", "revisao": "", "observacao": ""})
        for l in linhas:
            w("-" * 100)
            for k, v in l.items():
                w(f"  {k}: {v}")
        for sid, esp in (par.get("controle_esperado") or {}).items():
            self.controle(ex, inicio, int(sid), esp)
        usos = UsoIA.objects.filter(pesquisa=p, criado_em__gte=inicio)
        cons = ConsultaWeb.objects.filter(pesquisa=p, criada_em__gte=inicio)
        w("=" * 100)
        w(f"Chamadas de IA: {usos.exclude(status__in=['NAO_CONFIGURADA', 'SEM_ORCAMENTO']).count()} "
          f"({', '.join(f'{u.tipo}:{u.status}' for u in usos) or 'nenhuma'})")
        reais = cons.exclude(status__in=["NAO_CONFIGURADA", "SEM_ORCAMENTO"])
        w(f"Pesquisas web: {reais.count()} (ações contadas: {reais.aggregate(s=Sum('chamadas_ferramenta'))['s'] or 0})")
        for c in cons:
            concl = sum(1 for a in (c.acoes or []) if a.get("status") == "completed")
            w(f"  - {c.finalidade} {c.status}: {c.consulta[:80]} | {c.chamadas_ferramenta} ação(ões) "
              f"({concl} concluída(s)), {len(c.fontes or [])} fonte(s), {c.tokens_entrada}+{c.tokens_saida} tokens, "
              f"{c.duracao_ms} ms, US$ {c.custo_estimado_usd} (reservado {c.custo_reservado_usd})"
              + (f" | {c.erro[:100]}" if c.erro else ""))
            for r in (c.resultados or [])[:5] if isinstance(c.resultados, list) else []:
                w(f"      {r.get('url', '')[:90]} [{r.get('tipo', '')}] -> {r.get('leitura', 'sem leitura')[:110]}")
        soma = lambda qs, f: qs.aggregate(s=Sum(f))["s"] or 0
        custo = soma(usos, "custo_estimado_usd") + soma(cons, "custo_estimado_usd")
        w(f"Sites novos: {tot['sites']} | contatos novos além dos cadastrais: {tot['novos']} | corroborados: "
          f"{tot['corroborados']} | incorretos: {tot['incorretos']} | empresas com canal utilizável: "
          f"{tot['abordagem']} de {len(linhas)}")
        w(f"Consumo: {soma(usos, 'tokens_entrada') + soma(cons, 'tokens_entrada')} tokens de entrada + "
          f"{soma(usos, 'tokens_saida') + soma(cons, 'tokens_saida')} de saída; "
          f"{(soma(usos, 'duracao_ms') + soma(cons, 'duracao_ms')) / 1000:.1f} s; custo calculado pelo uso retornado "
          f"US$ {custo:.5f} (teto da etapa US$ {par.get('teto_usd')}; cobrança a confirmar no painel da OpenAI)")
        w(f"Gasto registrado na aplicação: US$ {orcm.gasto_registrado():.5f} (teto global US$ {orcm.teto_global()})")
        novas = SelecaoPesquisa.objects.filter(pesquisa=p, primeira_execucao=ex)
        for n in novas:
            w(f"  Descoberta: [{n.pk}] {n.cnpj} fila {n.fila} – {'; '.join(map(str, n.motivos_selecao or []))[:150]}")
        if novas:
            w("  Novas seleções ficam AGUARDANDO (não investigadas).")
        self._gravar(caminho_csv, linhas, "Tabela")
        self._gravar(caminho_planilha, planilha, "Planilha de revisão (preencha 'revisao' com CERTO, ERRADO ou NAO_SEI)")

    def controle(self, ex, inicio, sel_id, esp):
        w = self.stdout.write
        s = SelecaoPesquisa.objects.select_related("empresa").get(pk=sel_id)
        e = s.empresa
        assoc = {dominio_de_url(c.valor) for c in e.canais.filter(tipo="SITE", coletado_em__gte=inicio,
                                                                   confianca_associacao__in=["ALTA", "MEDIA"])}
        fontes = set()
        for c in ConsultaWeb.objects.filter(empresa=e, criada_em__gte=inicio):
            fontes |= {dominio_de_url(u) for u in c.fontes or []}
        esperados = set(esp["sites"])
        if esperados & assoc:
            veredito = "ACERTO: a pesquisa reencontrou o site e a associação foi confirmada"
        elif esperados & fontes:
            veredito = "PARCIAL: o site esperado veio nas fontes, mas não foi associado"
        else:
            veredito = "ERRO: a pesquisa não reencontrou o site esperado"
        recontatos = set(e.canais.filter(coletado_em__gte=inicio, valor__in=esp["contatos"]).values_list("valor", flat=True))
        outros = [d for d in assoc if d not in esperados]
        w("=" * 100)
        w(f"CONTROLE [{sel_id}] {e.nome}: esperado {sorted(esperados)}; associado agora {sorted(assoc) or '-'}")
        w(f"  {veredito}; contatos já conhecidos reencontrados: {len(recontatos)} de {len(esp['contatos'])}"
          + (f"; ATENÇÃO, outro site associado: {outros}" if outros else ""))

    def _gravar(self, caminho, linhas, rotulo):
        if not caminho or not linhas:
            return
        with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
            wr = csv.DictWriter(f, fieldnames=list(linhas[0].keys()), delimiter=";")
            wr.writeheader()
            wr.writerows(linhas)
        self.stdout.write(f"{rotulo}: {caminho}")

    # ------------------------------------------------------------------ revisão humana
    def importar_revisao(self, caminho):
        w = self.stdout.write
        cont = {"CERTO": 0, "ERRADO": 0, "NAO_SEI": 0, "ignorado": 0}
        with open(caminho, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f, delimiter=";"):
                rev = (row.get("revisao") or "").strip().upper().replace("Ã", "A").replace(" ", "_")
                if rev not in ("CERTO", "ERRADO", "NAO_SEI"):
                    cont["ignorado"] += bool(rev)
                    continue
                cont[rev] += 1
                if rev == "NAO_SEI":
                    continue
                c = CanalEmpresa.objects.filter(pk=int(row["canal_id"])).first()
                if not c:
                    continue
                if c.confirmacao_humana == CanalEmpresa.Confirmacao.ATENDIDO and rev == "CERTO":
                    continue  # já confirmado em contato: não rebaixa
                c.confirmacao_humana, c.confirmado_em = REVISAO[rev], timezone.now()
                obs = (row.get("observacao") or "").strip()
                if obs:
                    c.evidencia_associacao = f"{c.evidencia_associacao} | revisão da equipe: {obs}"[:500]
                c.save(update_fields=["confirmacao_humana", "confirmado_em", "evidencia_associacao"])
        from prospeccao.services.selecao import recalcular_filas
        for p in Pesquisa.objects.filter(modo="PLANEJADA"):
            recalcular_filas(p)
        total = cont["CERTO"] + cont["ERRADO"]
        w(f"Revisão importada: CERTO {cont['CERTO']}, ERRADO {cont['ERRADO']}, NÃO SEI {cont['NAO_SEI']}, "
          f"valores não reconhecidos {cont['ignorado']}")
        if total:
            w(f"Precisão nos itens revisados: {cont['CERTO'] / total:.0%} ({cont['CERTO']} de {total})")
