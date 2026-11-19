"""
Confere as integrações PAGAS (IA e busca web) sem exibir segredos.

    python manage.py verificar_integracoes                                # só configuração (nenhuma chamada)
    python manage.py verificar_integracoes --chamar --teto-usd 0.05       # + chamada mínima de IA (centésimos de US$)
    python manage.py verificar_integracoes --chamar --teto-usd 0.10 --com-busca   # + 1 pesquisa web (1 ação)

Chamadas reais só com --chamar E --teto-usd (o custo máximo é reservado antes; se não couber, nada é enviado).
Registradas em Usos de IA (tipo TESTE) e Consultas web (finalidade TESTE).
"""
import os
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError

ONDE = "arquivo .env na pasta do projeto (mesma pasta do manage.py)"


def _tem(nome):
    return "definida" if os.getenv(nome, "").strip() else "AUSENTE"


class Command(BaseCommand):
    help = "Verifica a configuração da IA (OpenAI) e da busca web; chamadas reais só com --chamar e --teto-usd."

    def add_arguments(self, parser):
        parser.add_argument("--chamar", action="store_true", help="Faz chamadas mínimas reais (exige --teto-usd)")
        parser.add_argument("--teto-usd", help="Teto financeiro desta verificação, ex.: 0.05")
        parser.add_argument("--com-busca", action="store_true", help="Inclui 1 pesquisa web real (1 ação)")

    def handle(self, *args, **opts):
        from prospeccao.conectores.busca_web import configurado as brave_ok
        from prospeccao.conectores.busca_web import obter_busca
        from prospeccao.services import ia, orcamento
        w = self.stdout.write
        prov = ia.provedor() or "(vazio)"
        w(f"Provedor de IA: {prov}")
        w(f"Modelo configurado: {ia.modelo_configurado() or '(nenhum)'}"
          + ("" if os.getenv("PROSPECCAO_IA_MODELO", "").strip() else " (padrão do provedor)"))
        w("Variáveis (valores não são exibidos):")
        for n in ("PROSPECCAO_IA_PROVEDOR", "OPENAI_API_KEY", "PROSPECCAO_IA_MODELO", "PROSPECCAO_BUSCA_PROVEDOR",
                  "SERPAPI_API_KEY", "PROSPECCAO_TETO_GLOBAL_USD", "BRAVE_SEARCH_API_KEY"):
            w(f"  {n}: {_tem(n)}")
        ok_ia, msg_ia = ia.status()
        w(f"IA: {msg_ia}")
        busca, msg_busca = obter_busca()
        w(msg_busca + ("" if busca is None else f" (modelo {getattr(busca, 'modelo', '-')})"))
        if not brave_ok():
            w("  Brave: opcional, não configurada (contratação própria da Brave; a chave da OpenAI não a habilita)")
        from prospeccao.models import ConsultaWeb, UsoIA
        ult_ia = UsoIA.objects.filter(tipo="TESTE", status="OK", provedor=ia.provedor()).order_by("-criado_em").first()
        ult_b = ConsultaWeb.objects.filter(finalidade="TESTE", status="OK",
                                           provedor=getattr(busca, "codigo", "-")).order_by("-criada_em").first()
        w("Situação: " + ("configuração ENCONTRADA" if ok_ia else "configuração INCOMPLETA") + "; validação por chamada real: "
          + (f"IA OK em {ult_ia.criado_em:%d/%m/%Y %H:%M} ({ult_ia.modelo})" if ult_ia else "IA NÃO validada")
          + "; " + (f"pesquisa web OK em {ult_b.criada_em:%d/%m/%Y %H:%M}" if ult_b else "pesquisa web NÃO validada"))
        if busca is not None and getattr(busca, "lote", False):
            w(f"Limites da pesquisa web: max_tool_calls={busca.max_acoes} ações/chamada (aplicado pela API), "
              f"max_output_tokens={busca.max_tokens}, sem retentativa, reserva por chamada US$ {busca.custo_maximo()}")
        tg = orcamento.teto_global()
        w(f"Teto global: {'US$ ' + str(tg) if tg is not None else 'não definido'}; "
          f"gasto registrado no sistema: US$ {orcamento.gasto_registrado():.4f}")
        if ia.provedor() == "openai" and not os.getenv("OPENAI_API_KEY", "").strip():
            w(self.style.WARNING(
                f"OpenAI aguardando configuração: preencha OPENAI_API_KEY no {ONDE} e reinicie o servidor/processador. "
                "Nenhuma etapa de IA será apresentada como executada enquanto isso."))
        if not opts["chamar"]:
            return
        if not opts["teto_usd"]:
            raise CommandError("Chamadas reais exigem --teto-usd (ex.: --teto-usd 0.05). Nenhuma chamada foi feita.")
        try:
            teto = Decimal(opts["teto_usd"])
        except InvalidOperation:
            raise CommandError("--teto-usd inválido")
        if not ok_ia:
            raise CommandError(f"IA não está pronta ({msg_ia}). Nenhuma chamada foi feita.")
        orc = orcamento.Orcamento(None, teto=teto, tipo="TESTE")
        self._testar_ia(ia, orcamento, orc)
        if opts["com_busca"]:
            if busca is None:
                raise CommandError(f"{msg_busca}. Nenhuma pesquisa web foi feita.")
            self._testar_busca(busca, orcamento, orc)
        w(f"Gasto desta verificação: US$ {orc.gasto:.5f} (teto US$ {teto})")

    def _testar_ia(self, ia, orcamento, orc):
        from prospeccao.models import UsoIA
        an = ia.obter_analisador()
        an.max_tokens = 800  # resposta curta (inclui raciocínio); sem retentativa automática
        self.stdout.write(f"Chamada mínima de IA: max_output_tokens={an.max_tokens}, retentativas={an.max_retries}")
        uso = UsoIA(tipo="TESTE", provedor=an.nome, modelo=an.modelo, versao_prompt="teste-v1")
        esquema = {"type": "object", "additionalProperties": False, "required": ["ok"],
                   "properties": {"ok": {"type": "boolean"}}}
        try:
            if an.nome == "openai":
                info = an.client.models.retrieve(an.modelo)  # sem custo: confirma que o modelo existe para a conta
                self.stdout.write(f"Modelo disponível na conta: {getattr(info, 'id', an.modelo)}")
            (bruto, usage, modelo), reserva, inicio = orcamento.executar_ia(
                orc, an, uso, "Responda ok=true.",
                lambda: an._gerar("Responda em JSON.", "Responda ok=true.", esquema, "teste"))
        except orcamento.SemOrcamento as e:
            raise CommandError(f"Teto insuficiente para a chamada mínima: {e}. Nada foi enviado.")
        except Exception as e:
            raise CommandError(f"IA: falha na chamada de validação ({type(e).__name__}: {str(e)[:200]})")
        uso.status, uso.modelo, uso.resultado = "OK", modelo, bruto or {}
        ia.registrar_uso(uso, usage, inicio)
        orc.liquidar(reserva, uso.custo_estimado_usd, ia=True)
        self.stdout.write(self.style.SUCCESS(
            f"IA OK: modelo {modelo}, saída estruturada {bruto}, {uso.tokens_entrada}+{uso.tokens_saida} tokens, "
            f"US$ {uso.custo_estimado_usd} (reservado US$ {reserva}), {uso.duracao_ms} ms"))

    def _testar_busca(self, busca, orcamento, orc):
        from prospeccao.conectores.base import ContextoExecucao
        if getattr(busca, "codigo", "") == "google_serpapi":
            fr = busca.franquia(ContextoExecucao())  # plano e saldo reais (não consome busca)
            self.stdout.write(f"Franquia SerpApi: plano {fr.get('plan_name')}, {fr.get('searches_per_month')} buscas/mês, "
                              f"restantes {fr.get('total_searches_left')} (usadas no mês {fr.get('this_month_usage')}), "
                              f"limite/hora {fr.get('account_rate_limit_per_hour')}")
            res = orcamento.buscar(busca, "autopeças Mauá SP", finalidade="TESTE", orc=orc, ctx=ContextoExecucao(),
                                   contexto={"modalidade": "local"})
            reg = orc.ultimo_registro
            if reg.status != "OK":
                raise CommandError(f"Google (SerpApi): {reg.status} ({reg.erro[:200]})")
            a = (reg.acoes or [{}])[0]
            self.stdout.write(self.style.SUCCESS(
                f"Google OK ({busca.rotulo}): requisição {a.get('request_id')}, {len(res)} resultado(s) locais, "
                f"{reg.duracao_ms} ms, US$ {reg.custo_estimado_usd}"))
            self.stdout.write(f"  URL do Google consultada: {a.get('google_url')}")
            for r in res[:5]:
                self.stdout.write(f"  {r.get('nome_citado')} | {r.get('endereco_citado')} | {r.get('telefones')} | "
                                  f"{r.get('site') or '-'}")
            return
        if getattr(busca, "lote", False):
            busca.max_acoes, busca.max_tokens = 1, 1500  # 1 ação, resposta curta
            self.stdout.write(f"Pesquisa web de teste: max_tool_calls=1, max_output_tokens=1500, retentativas="
                              f"{busca.client.max_retries}, reserva US$ {busca.custo_maximo()} (estimativa "
                              f"conservadora: tokens de entrada da pesquisa não têm limite técnico)")
        res = orcamento.buscar(busca, "distribuidora de lubrificantes Guarulhos SP", finalidade="TESTE", orc=orc,
                               ctx=ContextoExecucao(), contexto={"cidade": "Guarulhos", "uf": "SP",
                                                                "regiao": "Guarulhos/SP"}
                               if getattr(busca, "lote", False) else None)
        reg = orc.ultimo_registro
        if reg.status != "OK":
            raise CommandError(f"Busca web: {reg.status} ({reg.erro[:200]}); custo registrado US$ {reg.custo_estimado_usd}")
        for a in reg.acoes or []:
            self.stdout.write(f"  ação {a.get('tipo') or '?'}: {a.get('consulta') or a.get('url') or '-'} "
                              f"({len(a.get('fontes') or [])} fonte(s) em sources)")
        for u in (reg.fontes or [])[:10]:
            self.stdout.write(f"  fonte: {u}")
        for r in res[:5]:
            self.stdout.write(f"  aceito: {r['url']} ({r['verificacao']}; tipo {r.get('tipo')})")
        self.stdout.write(self.style.SUCCESS(
            f"Busca web OK ({reg.provedor}): {len(res)} resultado(s) aceito(s), {len(reg.fontes)} fonte(s), "
            f"{reg.chamadas_ferramenta} ação(ões), US$ {reg.custo_estimado_usd} (reservado US$ "
            f"{reg.custo_reservado_usd}), {reg.duracao_ms} ms"))
