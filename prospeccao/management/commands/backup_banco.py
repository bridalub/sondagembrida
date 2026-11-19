"""
Cópia de segurança do banco desta aplicação (pg_dump dentro do container, compactado), FORA do disco do Docker.

    python manage.py backup_banco --destino "F:\\Backups\\BridaProspeccao"      # ou BACKUP_BANCO_DIR no .env
    python manage.py backup_banco --destino ... --manter 7                       # guarda as 7 mais recentes

- Confere o espaço livre no destino antes de começar (exige 2x o tamanho atual do banco).
- Não lê nem grava nada no Salesforce; não para containers; não mexe no disco virtual do Docker.
- Recuperação: ver docs/OPERACAO_DISCO_E_BACKUP.md.
"""
import gzip
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Cópia de segurança (pg_dump) do banco da aplicação, fora do disco do Docker."

    def add_arguments(self, parser):
        parser.add_argument("--destino", default=os.getenv("BACKUP_BANCO_DIR", ""))
        parser.add_argument("--container", default=os.getenv("POSTGRES_CONTAINER", "brida-prospeccao-pg"))
        parser.add_argument("--manter", type=int, default=7)

    def handle(self, *a, **o):
        if not o["destino"]:
            raise CommandError("Informe --destino (ou BACKUP_BANCO_DIR): uma pasta FORA do disco C: do Docker.")
        destino = Path(o["destino"])
        destino.mkdir(parents=True, exist_ok=True)
        with connection.cursor() as c:
            c.execute("select pg_database_size(current_database())")
            tamanho = c.fetchone()[0]
        livre = shutil.disk_usage(destino).free
        if livre < 2 * tamanho:
            raise CommandError(f"Espaço insuficiente no destino: {livre / 1024 ** 3:.1f} GB livres; "
                               f"necessário ~{2 * tamanho / 1024 ** 3:.1f} GB.")
        db = settings.DATABASES["default"]
        arquivo = destino / f"{db['NAME']}-{datetime.now():%Y%m%d-%H%M%S}.sql.gz"
        cmd = ["docker", "exec", o["container"], "pg_dump", "-U", db["USER"], "--no-owner", db["NAME"]]
        self.stdout.write(f"Gerando {arquivo} (banco com {tamanho / 1024 ** 2:.0f} MB)...")
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc, \
                gzip.open(arquivo, "wb") as saida:
            shutil.copyfileobj(proc.stdout, saida, 1024 * 1024)
            erro = proc.stderr.read().decode(errors="replace")
            proc.wait()
        if proc.returncode != 0 or arquivo.stat().st_size < 1024:
            arquivo.unlink(missing_ok=True)
            raise CommandError(f"pg_dump falhou ({proc.returncode}): {erro[:300]}")
        antigos = sorted(destino.glob(f"{db['NAME']}-*.sql.gz"))[:-o["manter"]] if o["manter"] > 0 else []
        for velho in antigos:
            velho.unlink()
        self.stdout.write(self.style.SUCCESS(f"Cópia gravada: {arquivo} ({arquivo.stat().st_size / 1024 ** 2:.0f} MB); "
                                             f"{len(antigos)} cópia(s) antiga(s) removida(s)."))
