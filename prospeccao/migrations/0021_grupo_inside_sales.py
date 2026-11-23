"""O perfil passou a se chamar Inside Sales: renomeia o grupo (ou junta os membros, se os dois existirem)."""
from django.db import migrations

ANTIGO, NOVO = "Insight Sales", "Inside Sales"


def renomear(apps, schema_editor, de=ANTIGO, para=NOVO):
    Group = apps.get_model("auth", "Group")
    velho = Group.objects.filter(name=de).first()
    if velho is None:
        Group.objects.get_or_create(name=para)
        return
    novo = Group.objects.filter(name=para).first()
    if novo is None:
        velho.name = para
        velho.save(update_fields=["name"])
        return
    for u in velho.user_set.all():
        u.groups.add(novo)
    velho.delete()


def desfazer(apps, schema_editor):
    renomear(apps, schema_editor, de=NOVO, para=ANTIGO)


class Migration(migrations.Migration):
    dependencies = [("prospeccao", "0020_atendimento_e_papeis"), ("auth", "0012_alter_user_first_name_max_length")]
    operations = [migrations.RunPython(renomear, desfazer)]
