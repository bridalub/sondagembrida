from django.apps import AppConfig


class ProspeccaoConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "prospeccao"
    verbose_name = "Prospecção"

    def ready(self):
        from django.contrib.auth import get_user_model
        from django.db.models.signals import m2m_changed

        def grupo_adm(sender, instance, action, pk_set, **kwargs):
            """Colocar alguém no grupo ADM dá acesso total (inclusive a /admin/ para cadastrar usuários);
            tirar do grupo volta a Inside Sales."""
            if action not in ("post_add", "post_remove", "post_clear") or not hasattr(instance, "is_staff"):
                return
            from prospeccao.papeis import GRUPO_ADM
            no_adm = instance.groups.filter(name=GRUPO_ADM).exists()
            if no_adm and not (instance.is_staff and instance.is_superuser):
                type(instance).objects.filter(pk=instance.pk).update(is_staff=True, is_superuser=True)
            elif not no_adm and action != "post_add" and instance.is_staff:
                type(instance).objects.filter(pk=instance.pk).update(is_staff=False, is_superuser=False)

        m2m_changed.connect(grupo_adm, sender=get_user_model().groups.through, weak=False,
                            dispatch_uid="prospeccao_grupo_adm")
