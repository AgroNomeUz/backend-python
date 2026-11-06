from django.apps import AppConfig


class MarketplaceConfig(AppConfig):
    name = "marketplace"

    def ready(self):
        # Connects the handler that turns a succeeded payment into a paid order.
        from . import receivers  # noqa: F401
