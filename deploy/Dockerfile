# Imagem do sondagem-brida (web, worker e agendador usam a mesma imagem).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/Sao_Paulo

RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# Estáticos dentro da imagem (servidos pelo WhiteNoise). Chave só para este passo; a real vem do .env.
RUN DJANGO_SECRET_KEY=somente-build DJANGO_DEBUG=0 python manage.py collectstatic --noinput \
    && mkdir -p /app/media /app/logs \
    && chown -R app:app /app/media /app/logs

USER app
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120", \
     "--access-logfile", "-", "--forwarded-allow-ips", "*"]
