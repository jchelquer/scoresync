from django.apps import AppConfig


class PartiturasConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'partituras'

    def ready(self):
        from . import signals  # noqa: F401
