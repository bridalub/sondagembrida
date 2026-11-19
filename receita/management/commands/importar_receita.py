"""
Importa o recorte da base pública de CNPJ (dados abertos da Receita Federal).

    python manage.py importar_receita --competencia 2026-10 --uf SP --municipio GUARULHOS
    python manage.py importar_receita --competencia 2026-10 --uf SP                     # estado inteiro
    python manage.py importar_receita ... --dir D:\\receita --sem-download                # arquivos oficiais locais
    python manage.py importar_receita --competencia 2026-11 --uf SP --municipios-arquivo receita/municipios_atuacao.txt
    python manage.py importar_receita ... --apagar-arquivos                              # libera disco após cada arquivo

Rodar de novo o mesmo comando RETOMA uma carga interrompida (arquivos concluídos são pulados).
"""
from django.core.management.base import BaseCommand, CommandError

from receita import importador


class Command(BaseCommand):
    help = "Importa (em fluxo, com retomada) o recorte da base de CNPJ da Receita Federal."

    def add_arguments(self, parser):
        parser.add_argument("--competencia", required=True, help="AAAA-MM (pasta oficial)")
        parser.add_argument("--uf", action="append", required=True)
        parser.add_argument("--municipio", action="append", default=[], help="Nome do município (repetível)")
        parser.add_argument("--municipios-arquivo", help="Arquivo texto com um município por linha (ex.: "
                                                           "receita/municipios_atuacao.txt)")
        parser.add_argument("--dir", help="Pasta dos arquivos (padrão: %%LOCALAPPDATA%%\\BridaProspeccao\\receita)")
        parser.add_argument("--sem-download", action="store_true", help="Usar só arquivos locais já baixados")
        parser.add_argument("--apagar-arquivos", action="store_true", help="Apagar cada zip após processar")
        parser.add_argument("--so-ativas", action="store_true",
                            help="Base enxuta: grava só estabelecimentos ATIVOS (as baixadas/inaptas não viram lead)")
        parser.add_argument("--forcar", action="store_true", help="Recarregar mesmo se já houver carga concluída")

    def handle(self, *args, **o):
        if o.get("municipios_arquivo"):
            with open(o["municipios_arquivo"], encoding="utf-8") as f:
                o["municipio"] = list(o["municipio"]) + [l.strip() for l in f if l.strip() and not l.startswith("#")]
        try:
            carga = importador.importar(o["competencia"], o["uf"], o["municipio"], diretorio=o["dir"],
                                        baixar=not o["sem_download"], manter_arquivos=not o["apagar_arquivos"],
                                        forcar=o["forcar"], log=self.stdout.write, so_ativas=o["so_ativas"])
        except importador.CargaEmAndamento as e:
            raise CommandError(str(e))
        self.stdout.write(self.style.SUCCESS(
            f"Carga {carga.pk}: {carga.get_status_display()} · {carga.estabelecimentos:,} estabelecimentos · "
            f"{carga.empresas_complementadas:,} complementados · {carga.linhas_lidas:,} linhas lidas · "
            f"{carga.linhas_malformadas} malformadas · {carga.cnpj_invalidos} CNPJ inválidos"))
