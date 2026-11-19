"""
Normalização e validação de CNPJ, incluindo o formato alfanumérico
(12 primeiros caracteres [0-9A-Z] + 2 dígitos verificadores numéricos).
Valor de cada caractere no cálculo do DV = código ASCII - 48
(dígitos 0-9 continuam valendo 0-9; 'A' vale 17, 'B' 18, ...).
"""
import re
from dataclasses import dataclass

from carteira.models import StatusCNPJ

_FORMATO = re.compile(r"^[0-9A-Z]{12}[0-9]{2}$")


def normalizar(valor):
    """Remove pontuação/espaços e passa para maiúsculas. Não completa zeros."""
    if valor is None:
        return ""
    return re.sub(r"[^0-9A-Za-z]", "", str(valor)).upper()


def _dv(base):
    soma, peso = 0, 2
    for c in reversed(base):
        soma += (ord(c) - 48) * peso
        peso = 2 if peso == 9 else peso + 1
    r = soma % 11
    return 0 if r < 2 else 11 - r


def calcular_dv(base12):
    d1 = _dv(base12)
    d2 = _dv(base12 + str(d1))
    return f"{d1}{d2}"


def cnpj_valido(cnpj):
    if not cnpj or not _FORMATO.match(cnpj) or len(set(cnpj)) == 1:
        return False
    return cnpj[12:] == calcular_dv(cnpj[:12])


def cpf_valido(cpf):
    if not re.fullmatch(r"\d{11}", cpf or "") or len(set(cpf)) == 1:
        return False
    for n in (9, 10):
        soma = sum(int(cpf[i]) * (n + 1 - i) for i in range(n))
        dv = (soma * 10) % 11 % 10
        if dv != int(cpf[n]):
            return False
    return True


@dataclass
class ResultadoCNPJ:
    cnpj: str | None  # normalizado e válido, ou None
    status: str
    aviso: str = ""


def analisar(valor):
    bruto = normalizar(valor)
    if not bruto:
        return ResultadoCNPJ(None, StatusCNPJ.AUSENTE)
    if cnpj_valido(bruto):
        return ResultadoCNPJ(bruto, StatusCNPJ.VALIDO)
    if len(bruto) == 11 and cpf_valido(bruto):
        return ResultadoCNPJ(None, StatusCNPJ.CPF, "Documento parece CPF (11 dígitos válidos)")
    # Campo numérico na origem pode ter perdido zeros à esquerda.
    # Só aceita o complemento se o dígito verificador confirmar.
    if bruto.isdigit() and 11 < len(bruto) < 14:
        completo = bruto.zfill(14)
        if cnpj_valido(completo):
            return ResultadoCNPJ(completo, StatusCNPJ.VALIDO, "Zeros à esquerda restaurados (DV confere)")
    return ResultadoCNPJ(None, StatusCNPJ.INVALIDO, f"CNPJ inválido: '{valor}'")
