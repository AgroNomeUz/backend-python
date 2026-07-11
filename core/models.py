"""
core/models.py
Shared abstract model bases used by all apps.
"""

import uuid

from django.db import models


class PublicIdModel(models.Model):
    """
    Adds a non-primary UUID exposed to API clients instead of the integer PK.

    Integer PKs stay internal (cheap joins, ordered index); `public_id` is the
    only identifier that leaves the backend. Look up API-supplied ids with
    `Model.objects.get(public_id=...)`, never by `pk`.
    """

    public_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
    )

    class Meta:
        abstract = True
