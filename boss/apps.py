from django.apps import AppConfig

 
class BossConfig(AppConfig):
    name = "boss"
    verbose_name = "Boss"
    default_auto_field = "django.db.models.BigAutoField"
    dpy_package = "boss.boss"