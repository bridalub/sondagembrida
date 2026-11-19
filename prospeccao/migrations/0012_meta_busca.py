from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("prospeccao", "0011_oportunidade_escopo"),
    ]

    operations = [
        migrations.AddField(
            model_name="execucao",
            name="pausa_solicitada",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="pesquisa",
            name="quantidade_desejada",
            field=models.PositiveIntegerField(
                default=10,
                help_text="Total de novos leads da pesquisa (1 a 100), somando frentes e municípios",
            ),
        ),
        migrations.AddIndex(
            model_name="candidato",
            index=models.Index(fields=["cnpj_candidato"], name="candidato_cnpj_idx"),
        ),
    ]
