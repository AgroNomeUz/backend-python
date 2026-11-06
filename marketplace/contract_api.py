"""
marketplace/contract_api.py
Supply contracts. Both parties read a contract and either can act on it, so each
write works out which side the caller is on. `contracts.manage` gates every
write: it binds the organization for months, which is more than `market.manage`
was granted for.
"""

from uuid import UUID

from asgiref.sync import sync_to_async
from django.db.models import Q
from django.shortcuts import aget_object_or_404, get_object_or_404
from ninja import Router
from ninja.pagination import LimitOffsetPagination, paginate

from users.models import OrgPermission, Organization
from users.permissions import readable_organization, writable_organization

from . import contract_services as services
from .models import Offer, Quote, Side, SupplyContract, active_offer_q
from .order_api import order_relations
from .order_services import Actor
from .schemas import (
    ContractAnswerIn,
    ContractCreateIn,
    ContractDetailOut,
    ContractOut,
    ContractTerminateIn,
    ContractTermsIn,
    OrderOut,
)

contracts_router = Router(tags=["Market contracts"])


def contract_relations(queryset):
    """Everything `ContractOut` serialises, joined up front."""
    return queryset.select_related(
        "product",
        "product__category",
        "provider_organization",
        "provider_organization__region",
        "customer_organization",
        "customer_organization__region",
        "offer",
        "quote",
        "deal",
    ).prefetch_related("versions")


def org_contracts(organization: Organization):
    return SupplyContract.objects.filter(
        Q(provider_organization=organization) | Q(customer_organization=organization)
    )


def _detail(pk: int) -> SupplyContract:
    return contract_relations(SupplyContract.objects.all()).get(pk=pk)


def _terms(data: ContractTermsIn) -> dict:
    return data.dict(exclude={"offer_id", "quote_id"})


@contracts_router.get("", response=list[ContractOut])
@paginate(LimitOffsetPagination)
async def list_contracts(request, status: str | None = None):
    organization = readable_organization(request, OrgPermission.VIEW_CONTRACTS)
    qs = contract_relations(org_contracts(organization))
    if status:
        qs = qs.filter(status=status)
    return qs


def _apply_propose(request, organization, data: ContractCreateIn) -> SupplyContract:
    offer = quote = None
    if data.offer_id:
        offer = get_object_or_404(
            Offer.objects.filter(active_offer_q()).select_related("organization", "product"),
            public_id=data.offer_id,
        )
    if data.quote_id:
        # Only a quote on the caller's own request.
        quote = get_object_or_404(
            Quote.objects.filter(request__customer_organization=organization),
            public_id=data.quote_id,
        )
    terms = _terms(data)
    if terms["unit_price"] is None:
        del terms["unit_price"]
    actor = Actor.from_request(request, organization, Side.CUSTOMER)
    contract = services.propose_contract(actor, offer=offer, quote=quote, terms=terms)
    return _detail(contract.pk)


@contracts_router.post("", response={201: ContractDetailOut})
async def propose_contract(request, data: ContractCreateIn):
    """
    The buyer proposes, from an `offer_id` or a `quote_id`. Nothing binds anyone
    until the seller accepts.
    """
    organization = writable_organization(request, OrgPermission.MANAGE_CONTRACTS)
    return 201, await sync_to_async(_apply_propose)(request, organization, data)


@contracts_router.get("/{contract_id}", response=ContractDetailOut)
async def get_contract(request, contract_id: UUID):
    """The contract and every version of its terms, accepted or not."""
    organization = readable_organization(request, OrgPermission.VIEW_CONTRACTS)
    return await aget_object_or_404(
        contract_relations(org_contracts(organization)), public_id=contract_id
    )


@contracts_router.get("/{contract_id}/orders", response=list[OrderOut])
@paginate(LimitOffsetPagination)
async def list_contract_orders(request, contract_id: UUID):
    """The deliveries generated so far, soonest first."""
    organization = readable_organization(request, OrgPermission.VIEW_CONTRACTS)
    contract = await aget_object_or_404(org_contracts(organization), public_id=contract_id)
    return order_relations(contract.orders.all()).order_by("delivery_date")


def _act(request, organization, contract, operation, *args) -> SupplyContract:
    side = services.side_of(contract, organization)
    actor = Actor.from_request(request, organization, side)
    operation(actor, contract, *args)
    return _detail(contract.pk)


async def _write(request, contract_id: UUID, operation, *args) -> SupplyContract:
    organization = writable_organization(request, OrgPermission.MANAGE_CONTRACTS)
    contract = await aget_object_or_404(org_contracts(organization), public_id=contract_id)
    return await sync_to_async(_act)(request, organization, contract, operation, *args)


@contracts_router.post("/{contract_id}/accept", response=ContractDetailOut)
async def accept_contract(request, contract_id: UUID, data: ContractAnswerIn):
    """Sign the version you read. You cannot accept your own proposal."""
    return await _write(request, contract_id, services.accept_terms, data.version)


@contracts_router.post("/{contract_id}/reject", response=ContractDetailOut)
async def reject_contract(request, contract_id: UUID, data: ContractAnswerIn):
    """Refuse the version you read. An active contract carries on."""
    return await _write(request, contract_id, services.reject_terms, data.version)


@contracts_router.post("/{contract_id}/amend", response=ContractDetailOut)
async def amend_contract(request, contract_id: UUID, data: ContractTermsIn):
    """
    Propose new terms: a counter-offer before signing, a change after it. They
    apply only once the other side accepts, and only from their `start_date`.
    """
    return await _write(request, contract_id, services.amend_contract, _terms(data))


@contracts_router.post("/{contract_id}/terminate", response=ContractDetailOut)
async def terminate_contract(request, contract_id: UUID, data: ContractTerminateIn):
    """Give notice. Deliveries continue until the agreed notice runs out."""
    return await _write(request, contract_id, services.terminate_contract, data.reason)

