"""
farms/external_refs.py
External references for farm-side objects: the ids an object carries in other
systems (ADAPT, John Deere, ...), kept in `equipment.ExternalReference`.

That table is generic (content type + object id) and has no organization of
its own, so every access here starts from a target object the caller already
resolved through their own organization; a reference is only ever reachable
through its parent, which is what keeps another org's ids a 404.

Reusable for any org-owned target (farms now, fields and crop zones next):

  * `attach_references(targets)`  async, sets `.external_references` on each
  * `add_reference(...)` / `remove_reference(...)`  async, audited as an
    `updated` on the parent with a `changes["external_reference"]` entry
  * `add_reference_routes(router, resource, target_queryset)`  registers the
    three `/{id}/external-references` endpoints on a router
"""

from datetime import datetime
from uuid import UUID

from asgiref.sync import sync_to_async
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.shortcuts import aget_object_or_404, get_object_or_404
from ninja import Field, Schema
from ninja.errors import HttpError

from core.audit import log_activity
from core.models import ActivityLog
from equipment.models import ExternalReference
from users.permissions import caller_organization

from .common import writable_organization


class ExternalReferenceIn(Schema):
    # Typed against the enum so an unknown system is a 422 and the OpenAPI
    # schema lists the allowed values.
    system: ExternalReference.ExternalSystem
    external_id: str = Field(min_length=1, max_length=255)
    external_version: str = Field("", max_length=64)


class ExternalReferenceOut(Schema):
    id: UUID = Field(alias="public_id")
    system: str = Field(alias="external_system")
    external_id: str
    external_version: str
    created_at: datetime


def references_of(target):
    """Every reference attached to `target`, oldest first."""
    return ExternalReference.objects.filter(
        content_type=ContentType.objects.get_for_model(type(target)),
        object_id=target.pk,
    ).order_by("created_at", "id")


def _attach(targets: list) -> None:
    if not targets:
        return
    content_type = ContentType.objects.get_for_model(type(targets[0]))
    by_pk = {target.pk: [] for target in targets}
    rows = ExternalReference.objects.filter(
        content_type=content_type, object_id__in=by_pk
    ).order_by("created_at", "id")
    for row in rows:
        by_pk[row.object_id].append(row)
    for target in targets:
        target.external_references = by_pk[target.pk]


async def attach_references(targets: list) -> list:
    """
    Set `.external_references` on each target (all of one model) with a single
    query, so an async response can serialise them without a lazy fetch.
    """
    await sync_to_async(_attach)(targets)
    return targets


def _reference_label(ref: ExternalReference) -> str:
    return f"{ref.external_system}:{ref.external_id}"


def _apply_add_reference(request, organization, target, data: ExternalReferenceIn):
    """Sync transactional core of `add_reference`."""
    duplicate = HttpError(
        409, "This object already has a reference with that system and id"
    )
    if references_of(target).filter(
        external_system=data.system, external_id=data.external_id
    ).exists():
        raise duplicate

    try:
        with transaction.atomic():
            ref = ExternalReference.objects.create(
                content_type=ContentType.objects.get_for_model(type(target)),
                object_id=target.pk,
                external_system=data.system,
                external_id=data.external_id,
                external_version=data.external_version,
            )
            log_activity(
                request,
                organization,
                ActivityLog.Action.UPDATED,
                target,
                changes={
                    "external_reference": {"from": None, "to": _reference_label(ref)}
                },
            )
    except IntegrityError:
        # Lost a race with a concurrent identical request.
        raise duplicate
    return ref


async def add_reference(request, organization, target, data: ExternalReferenceIn):
    return await sync_to_async(_apply_add_reference)(
        request, organization, target, data
    )


def _apply_remove_reference(request, organization, target, ref_id: UUID) -> None:
    """Sync transactional core of `remove_reference`."""
    ref = get_object_or_404(references_of(target), public_id=ref_id)
    with transaction.atomic():
        log_activity(
            request,
            organization,
            ActivityLog.Action.UPDATED,
            target,
            changes={
                "external_reference": {"from": _reference_label(ref), "to": None}
            },
        )
        ref.delete()


async def remove_reference(request, organization, target, ref_id: UUID) -> None:
    await sync_to_async(_apply_remove_reference)(request, organization, target, ref_id)


def add_reference_routes(router, resource: str, target_queryset) -> None:
    """
    Register GET/POST `/{target_id}/external-references` and DELETE
    `/{target_id}/external-references/{ref_id}` on `router`.

    `target_queryset(organization)` returns the organization's own targets, so
    another org's id is a 404. `resource` ("farm", "field", ...) only names the
    operations, which django-ninja wants unique.
    """

    @router.get(
        "/{target_id}/external-references",
        response=list[ExternalReferenceOut],
        operation_id=f"list_{resource}_external_references",
    )
    async def list_external_references(request, target_id: UUID):
        target = await aget_object_or_404(
            target_queryset(caller_organization(request)), public_id=target_id
        )
        # `references_of` resolves the ContentType, which may hit the database.
        return await sync_to_async(lambda: list(references_of(target)))()

    @router.post(
        "/{target_id}/external-references",
        response={201: ExternalReferenceOut},
        operation_id=f"add_{resource}_external_reference",
    )
    async def add_external_reference(request, target_id: UUID, data: ExternalReferenceIn):
        organization = writable_organization(request)
        target = await aget_object_or_404(
            target_queryset(organization), public_id=target_id
        )
        return 201, await add_reference(request, organization, target, data)

    @router.delete(
        "/{target_id}/external-references/{ref_id}",
        response={204: None},
        operation_id=f"delete_{resource}_external_reference",
    )
    async def delete_external_reference(request, target_id: UUID, ref_id: UUID):
        organization = writable_organization(request)
        target = await aget_object_or_404(
            target_queryset(organization), public_id=target_id
        )
        await remove_reference(request, organization, target, ref_id)
        return 204, None
