"""
Configurações do projeto. Tudo que é sensível vem de variáveis de ambiente
(arquivo .env na raiz, ver .env.example). Nada de senha/token no código.
"""
import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
# Credenciais do Salesforce ficam no env.env já usado pela automação (Excel.py); nunca no código.
load_dotenv(BASE_DIR / "env.env", override=False)

DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"
_EM_TESTE = len(sys.argv) > 1 and sys.argv[1] == "test"

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG or _EM_TESTE:
        SECRET_KEY = "dev-apenas-local-nao-usar-em-producao"
    else:
        raise ImproperlyConfigured("Defina DJANGO_SECRET_KEY no ambiente (.env).")

ALLOWED_HOSTS = [h.strip() for h in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
# VPS atrás de Nginx com HTTPS: DJANGO_CSRF_TRUSTED_ORIGINS=https://seu-dominio e DJANGO_HTTPS=1
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]
if os.getenv("DJANGO_HTTPS", "0") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "carteira",
    "prospeccao",
    "receita",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "prospeccao.papeis.PapelMiddleware",  # Insight Sales só vê as telas dele
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "prospeccao.papeis.contexto",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Banco: PostgreSQL por padrão. DB_ENGINE=sqlite só para testes/rodar local sem Postgres.
if os.getenv("DB_ENGINE", "postgresql").lower() == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("POSTGRES_DB", "carteira"),
            "USER": os.getenv("POSTGRES_USER", "postgres"),
            "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),
            "HOST": os.getenv("POSTGRES_HOST", "localhost"),
            "PORT": os.getenv("POSTGRES_PORT", "5432"),
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Arquivos enviados (orçamentos anexados). NÃO são servidos publicamente: só pela view protegida
# carteira:anexo_orcamento (login + quem registrou ou ADMIN). Na VPS, aponte para um disco com backup.
MEDIA_ROOT = Path(os.getenv("DJANGO_MEDIA_ROOT") or (BASE_DIR / "media"))
MEDIA_URL = "/arquivos/"
ANEXO_MAX_MB = int(os.getenv("ANEXO_MAX_MB") or 10)

LOGIN_URL = "login"
CSRF_FAILURE_VIEW = "config.csrf.falha_csrf"  # login antigo recarrega sozinho; demais formulários: página explicativa
LOGIN_REDIRECT_URL = "prospeccao:visao_geral"
LOGOUT_REDIRECT_URL = "login"

# ------------------------------------------------------------------
# Integração Salesforce (somente leitura)
# Credenciais: SF_USERNAME, SF_PASSWORD, SF_SECURITY_TOKEN, SF_INSTANCE_URL
# são lidas diretamente do ambiente em carteira/services/salesforce.py.

# Mesmo filtro de distribuidor usado pela automação (Excel.py).
SF_FILTRO_DISTRIBUIDOR = os.getenv("SF_FILTRO_DISTRIBUIDOR", "%BRIDA%")
# Situações de cliente Protheus importadas. Inclui 'Bloqueado' para que contas bloqueadas sejam
# reconhecidas no cruzamento e nunca reapareçam como oportunidade nova (o bloqueio fica explícito).
# Vazio = sem filtro de situação.
SF_SITUACOES_CLIENTE = [s.strip() for s in os.getenv("SF_SITUACOES_CLIENTE", "Ativo,Inativo,Bloqueado").split(",")
                        if s.strip()]
# Campos opcionais adicionais de Account (atributo=CampoSalesforce). Telefone, e-mail, fantasia e
# última compra já são lidos por padrão. Atributos aceitos: telefone, email, cnae, data_abertura,
# razao_social, nome_fantasia.
SF_ACCOUNT_CAMPOS_EXTRAS = os.getenv("SF_ACCOUNT_CAMPOS_EXTRAS", "")
# Início padrão do histórico de compras: o mesmo do relatório VENDAS da automação.
COMPRAS_DESDE_PADRAO = os.getenv("COMPRAS_DESDE_PADRAO", "2026-04-01")

# ------------------------------------------------------------------
# Prospecção
# Ao clicar "Executar agora", dispara um worker em segundo plano (processo separado, não bloqueia a
# requisição). Em produção, prefira deixar um worker permanente (manage.py prospeccao_worker) e desligar.
PROSPECCAO_DISPARAR_WORKER = os.getenv("PROSPECCAO_DISPARAR_WORKER", "1") == "1" and not _EM_TESTE

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"carteira": {"handlers": ["console"], "level": "INFO"},
                "prospeccao": {"handlers": ["console"], "level": "INFO"},
                "receita": {"handlers": ["console"], "level": "INFO"}},
}
