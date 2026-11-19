"""Identificação pendente da pesquisa, dentro de um lote SerpApi já autorizado."""
import json

from django.core.management.base import BaseCommand
from django.utils import timezone

from carteira.models import Empresa
from prospeccao.models import Execucao, Pesquisa, SelecaoPesquisa
from prospeccao.services import dimensoes as dm
from prospeccao.services.enriquecimento import registrar_dado
from prospeccao.services.identificacao_cnpj import completar_identificacao
from prospeccao.services.lote_identificacao import (
    abrir_lote, buscar_na_franquia, consulta_ja_feita, custo_ia, exibir_conhecidas, explicar_estouro_diario,
    franquia_permite, montar_consultas, nome_e_logradouro, saldo_lote, titulo_comercial,
)


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--pesquisa", type=int, default=11)
        parser.add_argument("--maximo", type=int, default=53)
        parser.add_argument("--execucao", type=int, default=0)

    def handle(self, *args, **opts):
        self.stdout.write("INICIO")
        self.stdout.flush()
        pesquisa = Pesquisa.objects.get(pk=opts["pesquisa"])
        auditoria = explicar_estouro_diario()
        self.stdout.write(auditoria)
        self.stdout.flush()
        conta = franquia_permite()
        self.stdout.write("FRANQUIA " + json.dumps(conta or {}, default=str))
        if conta is None:
            self.stdout.write("PAROU sem franquia incluída antes da primeira busca")
            return
        ia_antes_n, ia_antes = custo_ia(pesquisa)
        if opts["execucao"]:
            ex = Execucao.objects.get(pk=opts["execucao"], pesquisa=pesquisa)
        else:
            ex = abrir_lote(pesquisa, opts["maximo"], auditoria)
        ids = list(SelecaoPesquisa.objects.filter(
            pesquisa=pesquisa, empresa__isnull=False).exclude(
            fila__in=["CONHECIDA", "FORA", "NAO_CONTATAR"]).values_list("empresa_id", flat=True).distinct())
        self.stdout.write("ANTES " + json.dumps(dm.contagens(ids)))
        conhecidas = exibir_conhecidas(pesquisa)
        self.stdout.write(f"CONHECIDAS_EXIBIDAS {conhecidas}")
        empresas = list(Empresa.objects.filter(pk__in=ids).prefetch_related("canais"))
        sels = {s.empresa_id: s for s in SelecaoPesquisa.objects.filter(pesquisa=pesquisa, empresa_id__in=ids)}
        fila = []
        for empresa in empresas:
            if empresa.salesforce_id:
                registrar_dado(empresa, "motivo_encerramento_identificacao",
                               "Já comprovada na carteira Salesforce. Busca não realizada.", "identificacao", "")
                continue
            sel = sels.get(empresa.pk)
            info = completar_identificacao(empresa, sel, max_buscas=0, permitir_consulta_complementar=False)
            empresa.refresh_from_db()
            if empresa.cnpj or info.get("acao") in ("cnpj_vinculado", "conhecida_salesforce", "ja_resolvida", "revisto"):
                continue
            pontos = (0 if nome_e_logradouro(empresa.nome) else 3)
            pontos += 2 if empresa.canais.filter(tipo="TELEFONE").exists() else 0
            pontos += 2 if empresa.canais.filter(tipo="SITE").exists() else 0
            pontos += 1 if empresa.cidade else 0
            fila.append((pontos, empresa.pk))
        fila.sort(key=lambda item: (-item[0], item[1]))
        feitas = []
        parar = ""

        def uma(pk, segunda=False):
            nonlocal parar
            usadas, maximo = saldo_lote(ex)
            if usadas >= maximo:
                parar = "lote"
                return
            empresa = Empresa.objects.get(pk=pk)
            if empresa.cnpj or empresa.salesforce_id:
                return
            sel = sels.get(pk)
            variantes = [q for q in montar_consultas(empresa) if not consulta_ja_feita(empresa, q)]
            if not variantes:
                if not segunda:
                    registrar_dado(empresa, "motivo_encerramento_identificacao",
                                   "Consulta dirigida equivalente já estava concluída. Não foi repetida.", "identificacao", "")
                return
            consulta = variantes[0]
            estado, resultados, meta = buscar_na_franquia(consulta, ex, empresa)
            feitas.append({"empresa": pk, "consulta": consulta, "estado": estado,
                           "request_id": meta.get("request_id", ""), "n": len(resultados)})
            self.stdout.write(f"BUSCA {pk} {estado} {meta.get('request_id', '')} {consulta[:90]}")
            if estado in ("sem_franquia", "sem_lote"):
                parar = estado
                return
            if nome_e_logradouro(empresa.nome):
                for item in resultados:
                    titulo = titulo_comercial(
                        item.get("titulo") or item.get("nome_citado"), empresa.nome, empresa.endereco)
                    if not titulo:
                        continue
                    empresa.nome = titulo
                    empresa.save(update_fields=["nome", "atualizado_em"])
                    registrar_dado(empresa, "nome_comercial", titulo, "google_organico", item.get("url") or consulta)
                    break
            if resultados:
                completar_identificacao(empresa, sel, max_buscas=0, permitir_consulta_complementar=True)

        for _pontos, pk in fila:
            if parar:
                break
            uma(pk)
        if not parar:
            for _pontos, pk in fila:
                if parar:
                    break
                if not Empresa.objects.filter(pk=pk, cnpj__isnull=True).exists():
                    continue
                uma(pk, segunda=True)
        buscadas = {item["empresa"] for item in feitas if item["estado"] in ("ok", "falha")}
        for _pontos, pk in fila:
            if pk not in buscadas:
                continue
            empresa = Empresa.objects.filter(pk=pk, cnpj__isnull=True).first()
            if empresa is None:
                continue
            registrar_dado(
                empresa, "motivo_encerramento_identificacao",
                "Busca dirigida executada sem associação segura de CNPJ. Identificação permanece pendente.",
                "identificacao", "")
        ids = list(SelecaoPesquisa.objects.filter(
            pesquisa=pesquisa, empresa__isnull=False).exclude(
            fila__in=["CONHECIDA", "FORA", "NAO_CONTATAR"]).values_list("empresa_id", flat=True).distinct())
        ia_n, ia = custo_ia(pesquisa)
        usadas, maximo = saldo_lote(ex)
        self.stdout.write("DEPOIS " + json.dumps(dm.contagens(ids)))
        self.stdout.write(f"LOTE {usadas}/{maximo} PAROU {parar or 'trabalho'}")
        self.stdout.write(f"IA_ANTES {ia_antes_n} {ia_antes} IA_DEPOIS {ia_n} {ia} TETO {pesquisa.teto_custo_usd}")
        self.stdout.write("BUSCAS " + json.dumps(feitas, ensure_ascii=False))
        self.stdout.write("FRANQUIA_FIM " + json.dumps(franquia_permite() or {}, default=str))
