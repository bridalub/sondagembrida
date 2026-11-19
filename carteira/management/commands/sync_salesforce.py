"""
Sincroniza dados do Salesforce (SOMENTE LEITURA).

    python manage.py sync_salesforce                 # clientes + contatos + leads BRIDA + compras
    python manage.py sync_salesforce --clientes      # só clientes (inclui Bloqueados)
    python manage.py sync_salesforce --contatos --leads
    python manage.py sync_salesforce --compras --desde 2026-04-01
    python manage.py sync_salesforce --dry-run       # simula, não grava nada
"""
from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from carteira.models import Importacao, NotaFiscal
from carteira.services import importacao, salesforce


class Command(BaseCommand):
    help = "Importa clientes, contatos, leads BRIDA e notas fiscais do Salesforce, somente leitura."

    def add_arguments(self, parser):
        parser.add_argument("--clientes", action="store_true", help="Sincroniza clientes (Account)")
        parser.add_argument("--contatos", action="store_true", help="Sincroniza contatos (Contact)")
        parser.add_argument("--leads", action="store_true", help="Sincroniza leads BRIDA (Lead)")
        parser.add_argument("--compras", action="store_true", help="Sincroniza notas fiscais")
        parser.add_argument("--desde", help="Início do período de notas (AAAA-MM-DD). Padrão: COMPRAS_DESDE_PADRAO")
        parser.add_argument("--ultimos-dias", type=int,
                            help="Notas dos últimos N dias (para a sincronização de hora em hora; ignora --desde)")
        parser.add_argument("--sem-devolucoes", action="store_true", help="Não importa notas de devolução")
        parser.add_argument("--dry-run", action="store_true", help="Simula sem gravar")

    def handle(self, *args, **opts):
        escolhidos = [k for k in ("clientes", "contatos", "leads", "compras") if opts[k]]
        if not escolhidos:
            escolhidos = ["clientes", "contatos", "leads", "compras"]
        try:
            desde = date.fromisoformat(opts["desde"] or settings.COMPRAS_DESDE_PADRAO)
        except ValueError:
            raise CommandError("--desde deve estar no formato AAAA-MM-DD")
        if opts.get("ultimos_dias"):
            from datetime import timedelta
            from django.utils import timezone
            desde = timezone.localdate() - timedelta(days=max(1, opts["ultimos_dias"]))

        try:
            sf = salesforce.conectar()
        except Exception as e:
            self._registrar_falha(Importacao.Tipo.CLIENTES, f"Falha de conexão: {e}")
            raise CommandError(f"Não foi possível conectar ao Salesforce: {e}")
        origem = f"Salesforce {sf.sf_instance}"

        if "clientes" in escolhidos:
            self._clientes(sf, origem, opts["dry_run"])
        if "contatos" in escolhidos:
            self._simples(sf, origem, opts["dry_run"], Importacao.Tipo.CONTATOS, salesforce.buscar_contatos,
                          importacao.importar_contatos)
        if "leads" in escolhidos:
            self._simples(sf, origem, opts["dry_run"], Importacao.Tipo.LEADS, salesforce.buscar_leads,
                          importacao.importar_leads)
        if "compras" in escolhidos:
            tipos = [NotaFiscal.Tipo.VENDA] + ([] if opts["sem_devolucoes"] else [NotaFiscal.Tipo.DEVOLUCAO])
            self._compras(sf, origem, desde, tipos, opts["dry_run"])
        if not opts["dry_run"] and {"clientes", "leads", "compras"} & set(escolhidos):
            try:
                from prospeccao.services.cruzamento import reconciliar_selecoes
                from prospeccao.services.oportunidades import recruzar_todas
                self.stdout.write(f"Oportunidades reavaliadas contra a carteira atualizada: {recruzar_todas()}")
                self.stdout.write(f"Seleções reconciliadas com a carteira: {reconciliar_selecoes()}")
            except ImportError:
                pass
            if not opts["dry_run"]:
                try:
                    from prospeccao.services.catalogo import sincronizar_catalogo
                    n = sincronizar_catalogo()
                    self.stdout.write(f"Catálogo de categorias atualizado a partir das notas: {n} categorias.")
                except ImportError:
                    pass

    def _registrar_falha(self, tipo, msg):
        Importacao.objects.create(tipo=tipo, origem="Salesforce", status=Importacao.Status.FALHA,
                                  finalizado_em=timezone.now(), mensagem=msg, executado_por="sync_salesforce")

    def _clientes(self, sf, origem, dry_run):
        self.stdout.write("Clientes: verificando campos opcionais de Account (describe)...")
        desejados = salesforce.parse_campos_extras(settings.SF_ACCOUNT_CAMPOS_EXTRAS)
        try:
            extras, inexistentes, candidatos = salesforce.descrever_account(sf, desejados)
            registros, query = salesforce.buscar_accounts(sf, extras)
        except Exception as e:
            self._registrar_falha(Importacao.Tipo.CLIENTES, f"Falha na consulta: {e}")
            raise CommandError(f"Falha ao consultar Account: {e}")
        for attr, campo in inexistentes.items():
            self.stderr.write(f"  Campo opcional {campo} ({attr}) não existe/sem acesso em Account – ignorado.")
        self.stdout.write(f"Clientes: {len(registros)} contas lidas. Importando...")
        imp = importacao.importar_clientes(
            registros, origem=origem, extras=extras, executado_por="sync_salesforce", dry_run=dry_run,
            parametros={"consulta": query, "extras_confirmados": extras, "extras_inexistentes": inexistentes,
                        "campos_candidatos_account": candidatos},
        )
        self._resumo(imp)

    def _simples(self, sf, origem, dry_run, tipo, buscar, importar):
        try:
            registros, query = buscar(sf)
        except Exception as e:
            self._registrar_falha(tipo, f"Falha na consulta: {e}")
            raise CommandError(f"Falha ao consultar {tipo}: {e}")
        self.stdout.write(f"{tipo}: {len(registros)} registros lidos.")
        imp = importar(registros, origem=origem, executado_por="sync_salesforce", dry_run=dry_run)
        if not dry_run:
            imp.parametros = {"consulta": query}
            imp.save(update_fields=["parametros"])
        self._resumo(imp)

    def _compras(self, sf, origem, desde, tipos, dry_run):
        itens, consultas = [], []
        try:
            for tipo in tipos:
                recs, q = salesforce.buscar_itens_nota(sf, desde, tipo)
                self.stdout.write(f"Compras: {len(recs)} itens de nota ({tipo}) desde {desde:%d/%m/%Y}.")
                itens.extend(recs)
                consultas.append(q)
        except Exception as e:
            self._registrar_falha(Importacao.Tipo.COMPRAS, f"Falha na consulta: {e}")
            raise CommandError(f"Falha ao consultar notas: {e}")
        imp = importacao.importar_compras(itens, desde=desde, tipos=tipos, origem=origem,
                                          executado_por="sync_salesforce", dry_run=dry_run, consultas=consultas)
        self._resumo(imp)

    def _resumo(self, imp):
        self.stdout.write(self.style.SUCCESS(
            f"{imp.get_tipo_display()}: lidos={imp.lidos} criados={imp.criados} atualizados={imp.atualizados} "
            f"inalterados={imp.inalterados} rejeitados={imp.rejeitados} inconsistentes={imp.inconsistentes}"
        ))
        if imp.detalhes:
            self.stdout.write(f"  detalhes: {imp.detalhes}")
        if imp.mensagem:
            self.stdout.write(self.style.WARNING(f"  {imp.mensagem}"))
