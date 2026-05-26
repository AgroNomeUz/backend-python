from django.db import models
from django.conf import settings


class RefreshToken(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="refresh_tokens",
    )
    token = models.TextField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_revoked = models.BooleanField(default=False)

    class Meta:
        indexes = [models.Index(fields=["token"])]

    def __str__(self):
        return f"RefreshToken(user={self.user_id}, revoked={self.is_revoked})"
