"""
Importador dos dados abertos do CNPJ (Receita Federal).

Fonte oficial: https://arquivos.receitafederal.gov.br  (compartilhamento público -> Dados/Cadastros/CNPJ/AAAA-MM)
Layout: "Novo layout dos dados abertos do CNPJ" (Receita). Arquivos CSV ';' com aspas, codificação latin-1.

Os arquivos são NACIONAIS (não há recorte por cidade): os 10 arquivos de Estabelecimentos são lidos
inteiros, em fluxo, direto de dentro do zip, e só as linhas do recorte (UF/municípios) são gravadas.
Empresas (razão social, natureza, porte) são lidas em fluxo e cruzadas pelo CNPJ básico do recorte.
Sócios e Simples NÃO são importados (dados pessoais/societários desnecessários nesta etapa).

- Retomada: estado por arquivo (ArquivoCarga). Arquivo interrompido tem suas linhas parciais apagadas
  e é reprocessado; arquivos concluídos são pulados.
- Exclusividade: restrição única 'uma carga EXECUTANDO'; carga sem heartbeat há 20 min pode ser retomada.
- Versão anterior: a carga ativa só é trocada (atomicamente) quando a nova termina.
"""
import csv
import io
import logging
import os
import re
import zipfile
from datetime import date, timedelta
from pathlib import Path

import requests
from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from carteira.services import cnpj as cnpj_srv
from carteira.services.normalizacao import normalizar_texto
from receita.models import PORTES, SITUACOES, ArquivoCarga, CargaReceita, EstabelecimentoReceita, TabelaReceita

logger = logging.getLogger("receita")
USER_AGENT = "BridaProspeccao/0.1 (leitura de dados abertos CNPJ)"
ESTABELECIMENTOS = [f"Estabelecimentos{i}.zip" for i in range(10)]
EMPRESAS = [f"Empresas{i}.zip" for i in range(10)]
TABELAS = {"MUNICIPIO": "Municipios.zip", "CNAE": "Cnaes.zip", "MOTIVO": "Motivos.zip", "NATUREZA": "Naturezas.zip"}
LOTE = 2000
_CPF_FINAL = re.compile(r"\s+\d{11}$")


class CargaEmAndamento(Exception):
    pass


def _cfg(nome, padrao):
    return getattr(settings, nome, None) or os.getenv(nome) or padrao


def url_base(competencia):
    raiz = _cfg("RECEITA_WEBDAV_URL", "https://arquivos.receitafederal.gov.br/public.php/webdav/Dados/Cadastros/CNPJ")
    return f"{raiz.rstrip('/')}/{competencia}"


def _auth():
    return (_cfg("RECEITA_SHARE_TOKEN", "gn672Ad4CF8N6TK"), "")


def diretorio_padrao(competencia):
    base = _cfg("RECEITA_DIR", str(Path(os.getenv("LOCALAPPDATA", Path.home())) / "BridaProspeccao" / "receita"))
    return Path(base) / competencia


# ------------------------------------------------------------------ download

def tamanho_remoto(competencia, nome):
    r = requests.head(f"{url_base(competencia)}/{nome}", auth=_auth(), timeout=60, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return int(r.headers.get("Content-Length") or 0) or None


def data_publicacao(competencia):
    """Data de publicação dos arquivos oficiais (Last-Modified). None se indisponível."""
    from email.utils import parsedate_to_datetime
    try:
        r = requests.head(f"{url_base(competencia)}/{TABELAS['MUNICIPIO']}", auth=_auth(), timeout=60,
                          headers={"User-Agent": USER_AGENT})
        return parsedate_to_datetime(r.headers["Last-Modified"]).date()
    except Exception:
        return None


def garantir_arquivo(competencia, nome, diretorio, baixar=True, progresso=None):
    """Arquivo local completo (confere tamanho). Baixa com retomada (Range) se `baixar`."""
    destino = Path(diretorio) / nome
    if not baixar:  # modo "arquivos oficiais locais": nenhuma chamada de rede
        if not destino.exists():
            raise FileNotFoundError(f"{nome} ausente em {diretorio} (download desativado)")
        return destino, None
    esperado = None
    erro_consulta = None
    for tentativa in range(5):  # o servidor da Receita às vezes derruba a conexão: tenta de novo com espera
        try:
            esperado = tamanho_remoto(competencia, nome)
            erro_consulta = None
            break
        except Exception as e:
            erro_consulta = e
            logger.warning("consulta de %s falhou (%s), tentativa %s", nome, e, tentativa + 1)
            import time
            time.sleep(min(60, 5 * 2 ** tentativa))
    if erro_consulta is not None:
        if not destino.exists():
            raise ConnectionError(f"não foi possível consultar {nome} na fonte oficial: {erro_consulta}")
    if destino.exists() and (esperado is None or destino.stat().st_size == esperado):
        return destino, esperado
    if not baixar:
        raise FileNotFoundError(f"{nome} ausente/incompleto em {diretorio} e download desativado")
    parcial = destino.with_suffix(destino.suffix + ".part")
    for tentativa in range(5):
        inicio = parcial.stat().st_size if parcial.exists() else 0
        headers = {"User-Agent": USER_AGENT, **({"Range": f"bytes={inicio}-"} if inicio else {})}
        try:
            with requests.get(f"{url_base(competencia)}/{nome}", auth=_auth(), headers=headers, stream=True,
                              timeout=120) as r:
                if r.status_code not in (200, 206):
                    raise ConnectionError(f"HTTP {r.status_code}")
                modo = "ab" if r.status_code == 206 else "wb"
                with open(parcial, modo) as f:
                    for bloco in r.iter_content(1 << 20):
                        f.write(bloco)
                        if progresso:
                            progresso()
            if esperado is None or parcial.stat().st_size == esperado:
                parcial.replace(destino)
                return destino, esperado
        except (requests.RequestException, ConnectionError) as e:
            logger.warning("download %s falhou (%s), tentativa %s", nome, e, tentativa + 1)
    raise ConnectionError(f"download de {nome} não concluiu após 5 tentativas")


# ------------------------------------------------------------------ leitura

def _linhas_zip(caminho):
    with zipfile.ZipFile(caminho) as z:
        membro = z.namelist()[0]
        with z.open(membro) as bruto:
            yield from io.TextIOWrapper(bruto, encoding="latin-1", newline="")


def _parse(linha):
    return next(csv.reader([linha], delimiter=";", quotechar='"'))


def _data(v):
    v = (v or "").strip()
    if len(v) != 8 or not v.isdigit() or v == "00000000":
        return None
    try:
        return date(int(v[:4]), int(v[4:6]), int(v[6:]))
    except ValueError:
        return None


def _tel(ddd, num):
    ddd, num = re.sub(r"\D", "", ddd or ""), re.sub(r"\D", "", num or "")
    return f"{ddd}{num}" if ddd and len(num) >= 7 else ""


def carregar_tabelas(competencia, diretorio, baixar=True):
    for tipo, nome in TABELAS.items():
        if TabelaReceita.objects.filter(competencia=competencia, tipo=tipo).exists():
            continue
        caminho, _ = garantir_arquivo(competencia, nome, diretorio, baixar)
        objs = []
        for linha in _linhas_zip(caminho):
            campos = _parse(linha)
            if len(campos) >= 2:
                objs.append(TabelaReceita(competencia=competencia, tipo=tipo, codigo=campos[0].strip(),
                                          descricao=campos[1].strip()[:255]))
        TabelaReceita.objects.bulk_create(objs, batch_size=2000, ignore_conflicts=True)


def tabela(competencia, tipo):
    return dict(TabelaReceita.objects.filter(competencia=competencia, tipo=tipo).values_list("codigo", "descricao"))


def resolver_municipios(competencia, nomes):
    mun = tabela(competencia, "MUNICIPIO")
    alvo = {normalizar_texto(n) for n in nomes}
    achados = [{"codigo": c, "nome": d} for c, d in mun.items() if normalizar_texto(d) in alvo]
    faltando = alvo - {normalizar_texto(m["nome"]) for m in achados}
    if faltando:
        raise ValueError(f"município(s) não encontrados na tabela da Receita: {', '.join(sorted(faltando))}")
    return achados


def linha_para_estab(c, carga, arquivo, mun, cnaes, motivos):
    """c = 30 campos do layout de Estabelecimentos. Retorna (objeto|None, motivo_rejeicao)."""
    basico, ordem, dv = c[0].strip(), c[1].strip(), c[2].strip()
    bruto = f"{basico}{ordem}{dv}".upper()
    doc = cnpj_srv.analisar(bruto)
    if not doc.cnpj or doc.cnpj != bruto:
        return None, "cnpj"
    email = c[27].strip().lower()
    sec = " ".join(x.strip() for x in c[12].split(",") if x.strip())
    return EstabelecimentoReceita(
        carga=carga, arquivo=arquivo, cnpj=bruto, cnpj_basico=basico,
        matriz={"1": True, "2": False}.get(c[3].strip()), nome_fantasia=c[4].strip()[:255],
        situacao_codigo=c[5].strip()[:2], situacao=SITUACOES.get(c[5].strip(), ""), data_situacao=_data(c[6]),
        motivo_codigo=c[7].strip()[:3], motivo=motivos.get(c[7].strip(), "")[:200], data_abertura=_data(c[10]),
        cnae_principal=c[11].strip()[:7], cnae_descricao=cnaes.get(c[11].strip(), "")[:255],
        cnaes_secundarios=sec[:1000], tipo_logradouro=c[13].strip()[:40], logradouro=c[14].strip()[:160],
        numero=c[15].strip()[:20], complemento=c[16].strip()[:160], bairro=c[17].strip()[:80],
        cep=re.sub(r"\D", "", c[18])[:8], uf=c[19].strip()[:2], municipio_codigo=c[20].strip()[:4],
        municipio=mun.get(c[20].strip(), "")[:80], telefone1=_tel(c[21], c[22]), telefone2=_tel(c[23], c[24]),
        email=email[:160] if "@" in email else "", situacao_especial=c[28].strip()[:60],
    ), ""


# ------------------------------------------------------------------ carga

def _heartbeat(carga, **campos):
    campos["heartbeat_em"] = timezone.now()
    CargaReceita.objects.filter(pk=carga.pk).update(**campos)
    for k, v in campos.items():
        setattr(carga, k, v)


def obter_carga(competencia, ufs, municipios_nomes, diretorio, forcar=False, minutos_sem_sinal=20):
    """Cria ou retoma a carga do recorte. Levanta CargaEmAndamento se outra estiver executando."""
    descricao = f"{'/'.join(ufs)}: {', '.join(municipios_nomes) or 'todos os municípios'}"
    if len(descricao) > 255:  # recorte grande: resumo estável (mesma lista -> mesma descrição, para retomar)
        import hashlib
        chave = hashlib.sha1("|".join(sorted(m.upper() for m in municipios_nomes)).encode()).hexdigest()[:10]
        descricao = (f"{'/'.join(ufs)}: {len(municipios_nomes)} municípios ({', '.join(municipios_nomes[:4])}...) "
                     f"#{chave}")[:255]
    limite = timezone.now() - timedelta(minutes=minutos_sem_sinal)
    CargaReceita.objects.filter(status=CargaReceita.Status.EXECUTANDO, heartbeat_em__lt=limite).update(
        status=CargaReceita.Status.INTERROMPIDA, mensagem="Sem sinal do processo; pode ser retomada.")
    outra = CargaReceita.objects.filter(status=CargaReceita.Status.EXECUTANDO).first()
    if outra:
        raise CargaEmAndamento(f"Carga {outra.pk} ({outra}) já está executando")
    existente = (CargaReceita.objects.filter(competencia=competencia, descricao_filtro=descricao)
                 .exclude(status=CargaReceita.Status.SUBSTITUIDA).order_by("-iniciada_em").first())
    if existente and existente.status == CargaReceita.Status.CONCLUIDA and not forcar:
        return existente, False
    try:
        with transaction.atomic():
            if existente and existente.status != CargaReceita.Status.CONCLUIDA:
                existente.status, existente.mensagem = CargaReceita.Status.EXECUTANDO, ""
                existente.heartbeat_em = timezone.now()
                existente.save()
                return existente, True
            carga = CargaReceita.objects.create(competencia=competencia, filtro_ufs=ufs, descricao_filtro=descricao,
                                                origem=url_base(competencia), diretorio=str(diretorio))
    except IntegrityError:
        raise CargaEmAndamento("Outra carga começou ao mesmo tempo")
    return carga, True


def importar(competencia, ufs, municipios_nomes, *, diretorio=None, baixar=True, manter_arquivos=True,
             forcar=False, log=print, so_ativas=False):
    diretorio = Path(diretorio or diretorio_padrao(competencia))
    diretorio.mkdir(parents=True, exist_ok=True)
    ufs = [u.upper() for u in ufs]
    carga, iniciar = obter_carga(competencia, ufs, municipios_nomes, diretorio, forcar)
    if not iniciar:
        log(f"Carga {carga.pk} desta competência e recorte já está concluída (use --forcar para recarregar).")
        return carga
    try:
        _heartbeat(carga, etapa="tabelas")
        carregar_tabelas(competencia, diretorio, baixar)
        municipios = resolver_municipios(competencia, municipios_nomes) if municipios_nomes else []
        _heartbeat(carga, filtro_municipios=municipios,
                   data_arquivos=carga.data_arquivos or (data_publicacao(competencia) if baixar else None))
        log(f"Carga {carga.pk}: recorte {carga.descricao_filtro} -> códigos {[m['codigo'] for m in municipios]}")
        _estabelecimentos(carga, competencia, diretorio, ufs, municipios, baixar, manter_arquivos, log, so_ativas)
        _empresas(carga, competencia, diretorio, baixar, manter_arquivos, log)
        _ativar(carga, log)
    except Exception as e:
        _heartbeat(carga, status=CargaReceita.Status.FALHA, mensagem=f"{type(e).__name__}: {e}")
        raise
    return carga


def _arquivo(carga, nome, competencia):
    arq, _ = ArquivoCarga.objects.get_or_create(carga=carga, nome=nome)
    return arq


def _estabelecimentos(carga, competencia, diretorio, ufs, municipios, baixar, manter, log, so_ativas=False):
    mun = tabela(competencia, "MUNICIPIO")
    cnaes = tabela(competencia, "CNAE")
    motivos = tabela(competencia, "MOTIVO")
    codigos = {m["codigo"] for m in municipios}
    # pré-filtro barato na linha bruta: '"UF";"COD";' (campos 20 e 21 adjacentes no layout)
    padroes = [f'"{u}";"{c}";' for u in ufs for c in codigos] if codigos else [f'"{u}";"' for u in ufs]
    for nome in ESTABELECIMENTOS:
        arq = _arquivo(carga, nome, competencia)
        if arq.status == ArquivoCarga.Status.CONCLUIDO:
            log(f"  {nome}: já processado ({arq.linhas_gravadas} estabelecimentos) – pulado")
            continue
        apagados = EstabelecimentoReceita.objects.filter(carga=carga, arquivo=nome).delete()[0]
        if apagados:
            log(f"  {nome}: {apagados} linhas parciais de tentativa anterior removidas")
        arq.status, arq.iniciado_em, arq.erro, arq.linhas_lidas, arq.linhas_gravadas = \
            ArquivoCarga.Status.BAIXANDO, timezone.now(), "", 0, 0
        arq.save()
        _heartbeat(carga, etapa=f"baixando {nome}")
        caminho, arq.tamanho_esperado = garantir_arquivo(competencia, nome, diretorio, baixar,
                                                         progresso=lambda: None)
        arq.status = ArquivoCarga.Status.PROCESSANDO
        arq.save()
        _heartbeat(carga, etapa=f"processando {nome}")
        lote, lidas, gravadas, malformadas, invalidos = [], 0, 0, 0, 0
        try:
            for linha in _linhas_zip(caminho):
                lidas += 1
                if not any(p in linha for p in padroes):
                    if lidas % 2_000_000 == 0:
                        _heartbeat(carga)
                        ArquivoCarga.objects.filter(pk=arq.pk).update(linhas_lidas=lidas)
                    continue
                c = _parse(linha)
                if len(c) < 30:
                    malformadas += 1
                    continue
                if c[19].strip() not in ufs or (codigos and c[20].strip() not in codigos):
                    continue
                if so_ativas and c[5].strip().lstrip("0") != "2":  # base enxuta: só situação ATIVA (02)
                    continue
                obj, motivo = linha_para_estab(c, carga, nome, mun, cnaes, motivos)
                if obj is None:
                    invalidos += 1
                    continue
                lote.append(obj)
                if len(lote) >= LOTE:
                    EstabelecimentoReceita.objects.bulk_create(lote, ignore_conflicts=True)
                    gravadas += len(lote)
                    lote = []
            if lote:
                EstabelecimentoReceita.objects.bulk_create(lote, ignore_conflicts=True)
                gravadas += len(lote)
        except Exception as e:
            ArquivoCarga.objects.filter(pk=arq.pk).update(status=ArquivoCarga.Status.FALHA, erro=str(e)[:2000],
                                                          linhas_lidas=lidas)
            raise
        ArquivoCarga.objects.filter(pk=arq.pk).update(status=ArquivoCarga.Status.CONCLUIDO, concluido_em=timezone.now(),
                                                      linhas_lidas=lidas, linhas_gravadas=gravadas)
        _heartbeat(carga, linhas_lidas=carga.linhas_lidas + lidas, linhas_malformadas=carga.linhas_malformadas + malformadas,
                   cnpj_invalidos=carga.cnpj_invalidos + invalidos)
        log(f"  {nome}: {lidas:,} linhas lidas, {gravadas:,} no recorte, {malformadas} malformadas, {invalidos} CNPJ inválido")
        if not manter:
            Path(caminho).unlink(missing_ok=True)


def _atualizar_empresas_sql(carga, dados):
    """dados: [(basico, razao, natureza_cod, natureza, porte)]"""
    if connection.vendor == "postgresql":
        with connection.cursor() as cur:
            valores = ",".join(["(%s,%s,%s,%s,%s)"] * len(dados))
            params = [x for linha in dados for x in linha]
            cur.execute(
                f"UPDATE receita_estabelecimentoreceita e SET razao_social=v.r, natureza_codigo=v.nc, natureza=v.n, "
                f"porte=v.p FROM (VALUES {valores}) AS v(b, r, nc, n, p) "
                f"WHERE e.carga_id = %s AND e.cnpj_basico = v.b", params + [carga.pk])
            return cur.rowcount
    total = 0
    for b, r, nc, n, p in dados:
        total += EstabelecimentoReceita.objects.filter(carga=carga, cnpj_basico=b).update(
            razao_social=r, natureza_codigo=nc, natureza=n, porte=p)
    return total


def _empresas(carga, competencia, diretorio, baixar, manter, log):
    basicos = set(EstabelecimentoReceita.objects.filter(carga=carga).values_list("cnpj_basico", flat=True).distinct())
    naturezas = tabela(competencia, "NATUREZA")
    log(f"  {len(basicos):,} CNPJs básicos no recorte para complementar com razão social/natureza/porte")
    for nome in EMPRESAS:
        arq = _arquivo(carga, nome, competencia)
        if arq.status == ArquivoCarga.Status.CONCLUIDO:
            log(f"  {nome}: já processado – pulado")
            continue
        arq.status, arq.iniciado_em = ArquivoCarga.Status.BAIXANDO, timezone.now()
        arq.save()
        _heartbeat(carga, etapa=f"baixando {nome}")
        caminho, arq.tamanho_esperado = garantir_arquivo(competencia, nome, diretorio, baixar)
        ArquivoCarga.objects.filter(pk=arq.pk).update(status=ArquivoCarga.Status.PROCESSANDO,
                                                      tamanho_esperado=arq.tamanho_esperado)
        _heartbeat(carga, etapa=f"processando {nome}")
        lidas, atualizadas, lote = 0, 0, []
        for linha in _linhas_zip(caminho):
            lidas += 1
            b = linha[1:9]  # '"12345678";...' – teste barato antes do parse
            if b not in basicos:
                if lidas % 2_000_000 == 0:
                    _heartbeat(carga)
                continue
            c = _parse(linha)
            if len(c) < 6:
                continue
            razao = _CPF_FINAL.sub("", c[1].strip())  # MEI: remove CPF do fim da razão social
            lote.append((c[0].strip(), razao[:255], c[2].strip()[:4], naturezas.get(c[2].strip(), "")[:160],
                         PORTES.get(c[5].strip(), "")))
            if len(lote) >= 1000:
                atualizadas += _atualizar_empresas_sql(carga, lote)
                lote = []
        if lote:
            atualizadas += _atualizar_empresas_sql(carga, lote)
        ArquivoCarga.objects.filter(pk=arq.pk).update(status=ArquivoCarga.Status.CONCLUIDO, concluido_em=timezone.now(),
                                                      linhas_lidas=lidas, linhas_gravadas=atualizadas)
        _heartbeat(carga, empresas_complementadas=carga.empresas_complementadas + atualizadas)
        log(f"  {nome}: {lidas:,} linhas lidas, {atualizadas:,} estabelecimentos complementados")
        if not manter:
            Path(caminho).unlink(missing_ok=True)


def _ativar(carga, log):
    total = EstabelecimentoReceita.objects.filter(carga=carga).count()
    with transaction.atomic():
        anteriores = list(CargaReceita.objects.filter(ativa=True).exclude(pk=carga.pk))
        CargaReceita.objects.filter(pk__in=[c.pk for c in anteriores]).update(ativa=False,
                                                                               status=CargaReceita.Status.SUBSTITUIDA)
        _heartbeat(carga, status=CargaReceita.Status.CONCLUIDA, ativa=True, finalizada_em=timezone.now(),
                   estabelecimentos=total, etapa="concluída")
    for c in anteriores:  # só depois da troca: libera espaço da versão antiga
        EstabelecimentoReceita.objects.filter(carga=c).delete()
    log(f"Carga {carga.pk} concluída e ativada: {total:,} estabelecimentos no recorte."
        + (f" Cargas anteriores substituídas: {[c.pk for c in anteriores]}" if anteriores else ""))
