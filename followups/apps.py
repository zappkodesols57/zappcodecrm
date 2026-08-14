from django.apps import AppConfig


class FollowupsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'followups'

    def ready(self):
        import followups.signals  # noqa
