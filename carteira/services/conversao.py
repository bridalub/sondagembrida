"""Conversões de valores vindos da API do Salesforce."""
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation


def texto(v):
    if v is None:
        return ""
    return str(v).strip()


def vazio(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def data(v):
    """'2026-04-01' ou '2026-04-01T..' -> date. Inválido -> None (nunca 01/01/2000)."""
    if vazio(v):
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    try:
        return date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        return None


def data_hora(v):
    """Datetime ISO do Salesforce (sempre UTC na API) -> datetime aware."""
    if vazio(v):
        return None
    s = str(v).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt


def decimal(v):
    """A API usa ponto como separador decimal (mesma premissa de _para_float no Excel.py)."""
    if vazio(v):
        return None
    try:
        return Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None


def booleano(v):
    return v if isinstance(v, bool) else None


def sem_attributes(obj):
    """Remove as chaves 'attributes' que o simple_salesforce inclui em cada registro."""
    if isinstance(obj, dict):
        return {k: sem_attributes(v) for k, v in obj.items() if k != "attributes"}
    if isinstance(obj, list):
        return [sem_attributes(v) for v in obj]
    return obj
