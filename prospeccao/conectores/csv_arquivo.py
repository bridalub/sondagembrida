"""
IMPORTAÇÃO de arquivo CSV (alternativa operacional, NÃO é mineração automática).

Colunas reconhecidas (cabeçalho, ; ou ,): nome (obrigatória), cnpj, cidade, uf, telefone, email, site,
atividade, endereco, cep, referencia. Cada linha vira um candidato da fonte 'csv_importacao'.
"""
import csv
import hashlib
import io

from carteira.services import cnpj as cnpj_srv
from prospeccao.conectores.base import ResultadoDescoberta

COLUNAS = {"nome", "cnpj", "cidade", "uf", "telefone", "email", "site", "atividade", "endereco", "cep", "referencia"}


def ler_csv(conteudo_bytes, nome_arquivo=""):
    texto = None
    for enc in ("utf-8-sig", "cp1252"):
        try:
            texto = conteudo_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    amostra = texto[:2000]
    delim = ";" if amostra.count(";") >= amostra.count(",") else ","
    leitor = csv.DictReader(io.StringIO(texto), delimiter=delim)
    cab = {c: (c or "").strip().lower() for c in (leitor.fieldnames or [])}
    if "nome" not in cab.values():
        raise ValueError("o CSV precisa de uma coluna 'nome'")
    resultados, erros = [], []
    for n, linha in enumerate(leitor, start=2):
        lin = {cab[k]: (v or "").strip() for k, v in linha.items() if k in cab and cab[k] in COLUNAS}
        if not lin.get("nome"):
            erros.append(f"linha {n}: sem nome")
            continue
        doc = cnpj_srv.analisar(lin.get("cnpj")) if lin.get("cnpj") else None
        if lin.get("cnpj") and not doc.cnpj:
            erros.append(f"linha {n}: CNPJ inválido '{lin['cnpj']}' (mantido como candidato sem CNPJ)")
        chave = doc.cnpj if doc and doc.cnpj else hashlib.sha1(
            "|".join(lin.get(k, "").lower() for k in ("nome", "cidade", "uf", "telefone", "site")).encode()).hexdigest()[:20]
        resultados.append(ResultadoDescoberta(
            id_externo=f"csv:{chave}", nome=lin["nome"][:255], endereco=lin.get("endereco", "")[:255],
            cidade=lin.get("cidade", "")[:120], uf=lin.get("uf", "")[:2].upper(), cep=lin.get("cep", "")[:10],
            telefone=lin.get("telefone", "")[:60], email=lin.get("email", "")[:254], site=lin.get("site", "")[:300],
            atividade=lin.get("atividade", "")[:500], cnpj=doc.cnpj if doc and doc.cnpj else "",
            url_referencia=lin.get("referencia", "")[:500] if lin.get("referencia", "").startswith("http") else "",
            dados={"arquivo": nome_arquivo, "linha": n},
        ))
    return resultados, erros
