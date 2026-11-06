"""
marketplace/contract_services.py
Signing, amending and ending supply contracts, and turning them into orders.

The rule everything here serves: **no version of the terms takes effect until
the side that did not propose it accepts it.** Neither party can change a price
or a schedule alone, before or after signing.
"""

import logging
from datetime import date, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from ninja.errors import HttpError

from core.models import ActivityLog

from .models import (
    ContractTerms,
    Offer,
    Order,
    Quote,
    Side,
    SupplyContract,
    market_today,
)
from .order_services import (
    SYSTEM,
    Actor,
    apply_contract_terms,
    audit,
    cancel_order,
    contract_order_values,
    line_total,
    record_generated_order,
    require_may_sell,
)

logger = logging.getLogger(__name__)
ContractStatus = SupplyContract.Status
TermsStatus = ContractTerms.Status

# Statuses in which a delivery has not left the seller and can still be called off.
UNDISPATCHED = (
    Order.Status.PENDING_CONFIRMATION,
    Order.Status.AWAITING_PAYMENT,
    Order.Status.PAID,
)


def _locked(contract: SupplyContract) -> SupplyContract:
    return (
        SupplyContract.objects.select_for_update(of=("self",))
        .select_related("provider_organization", "customer_organization", "product")
        .get(pk=contract.pk)
    )


def latest_accepted(contract: SupplyContract) -> ContractTerms | None:
    return contract.versions.filter(status=TermsStatus.ACCEPTED).first()


def pending_terms(contract: SupplyContract) -> ContractTerms | None:
    return contract.versions.filter(status=TermsStatus.PROPOSED).first()


def _validate(terms: dict, *, earliest_start: date) -> None:
    if terms["start_date"] < earliest_start:
        raise HttpError(400, f"The start date cannot be before {earliest_start}.")
    if terms["end_date"] < terms["start_date"]:
        raise HttpError(400, "The end date cannot be before the start date.")
    if not schedule_has_a_date(terms):
        raise HttpError(400, "That schedule has no delivery between those dates.")
    if terms.get("delivery_method") == "seller_delivery":
        if not terms.get("delivery_address", "").strip():
            raise HttpError(400, "A delivery address is required.")
    line_total(terms["quantity_per_delivery"], terms["unit_price"])


def schedule_has_a_date(terms: dict) -> bool:
    day, end = terms["start_date"], terms["end_date"]
    # Enough to meet every weekday of the longest interval.
    for _ in range(7 * max(terms.get("every_n_weeks", 1), 1)):
        if day > end:
            return False
        if is_delivery_day(
            terms["delivery_weekdays"], terms.get("every_n_weeks", 1), terms["start_date"], day
        ):
            return True
        day += timedelta(days=1)
    return False


def is_delivery_day(weekdays, every_n_weeks: int, start: date, day: date) -> bool:
    """Weeks count from the Monday of the week the terms start, so a fortnightly
    schedule stays on the same weeks whichever weekday it began on."""
    if day.weekday() not in weekdays:
        return False
    first_monday = start - timedelta(days=start.weekday())
    weeks_since = (day - first_monday).days // 7
    return weeks_since % every_n_weeks == 0


def _add_version(
    contract: SupplyContract, actor: Actor, terms: dict, version: int
) -> ContractTerms:
    return ContractTerms.objects.create(
        contract=contract,
        version=version,
        proposed_by_side=actor.side,
        proposed_by=actor.user,
        **terms,
    )


@transaction.atomic
def propose_contract(
    actor: Actor, *, offer: Offer | None = None, quote: Quote | None = None, terms: dict
) -> SupplyContract:
    """The buyer proposes, from an offer or from a quote on their request."""
    if (offer is None) == (quote is None):
        raise HttpError(400, "Say which offer or quote this contract is based on.")

    if offer is not None:
        provider, product = offer.organization, offer.product
        variety, grade, unit = offer.variety, offer.grade, offer.unit
        terms.setdefault("unit_price", offer.price)
    else:
        quote = Quote.objects.select_for_update(of=("self",)).select_related(
            "provider_organization", "request", "request__product"
        ).get(pk=quote.pk)
        if quote.status != Quote.Status.SUBMITTED:
            raise HttpError(409, "This quote is no longer open.")
        if quote.valid_until < market_today():
            raise HttpError(409, "This quote has expired.")
        provider, product = quote.provider_organization, quote.request.product
        variety, grade, unit = quote.variety, quote.grade, quote.request.unit
        terms.setdefault("unit_price", quote.unit_price)
        quote.status = Quote.Status.ACCEPTED
        quote.save(update_fields=["status", "updated_at"])

    if provider.pk == actor.organization.pk:
        raise HttpError(400, "You cannot contract with your own organization.")
    require_may_sell(product, provider)
    _validate(terms, earliest_start=market_today())

    contract = SupplyContract.objects.create(
        provider_organization=provider,
        customer_organization=actor.organization,
        created_by=actor.user,
        offer=offer,
        quote=quote,
        product=product,
        variety=variety,
        grade=grade,
        unit=unit,
    )
    _add_version(contract, actor, terms, version=1)
    audit(
        actor,
        contract,
        ActivityLog.Action.CREATED,
        {"unit_price": {"from": None, "to": str(terms["unit_price"])}},
    )
    return contract


@transaction.atomic
def amend_contract(actor: Actor, contract: SupplyContract, terms: dict) -> ContractTerms:
    """
    A counter-offer before signing, or a change after it. Replaces whatever was
    waiting for an answer, and needs the other side like any proposal.
    """
    contract = _locked(contract)
    if contract.status not in (ContractStatus.PROPOSED, ContractStatus.ACTIVE):
        raise HttpError(409, "This contract has ended.")
    if contract.termination_date is not None:
        raise HttpError(409, "This contract is ending and cannot be changed.")

    earliest = market_today()
    if contract.status == ContractStatus.ACTIVE:
        # A change cannot reach back over a delivery already due.
        earliest += timedelta(days=1)
    _validate(terms, earliest_start=earliest)

    contract.versions.filter(status=TermsStatus.PROPOSED).update(
        status=TermsStatus.SUPERSEDED
    )
    highest = contract.versions.order_by("-version").values_list("version", flat=True)
    version = _add_version(contract, actor, terms, version=highest[0] + 1)
    audit(
        actor,
        contract,
        ActivityLog.Action.UPDATED,
        {"proposed_version": {"from": None, "to": version.version}},
    )
    return version


def accept_terms(actor: Actor, contract: SupplyContract) -> SupplyContract:
    """
    Sign. The first accepted version is what makes a contract active.

    Orders are brought into line after the commit, not inside it. See
    `sync_orders_with_terms`.
    """
    with transaction.atomic():
        contract = _locked(contract)
        terms = pending_terms(contract)
        if terms is None:
            raise HttpError(409, "Nothing is waiting to be accepted.")
        if terms.proposed_by_side == actor.side:
            raise HttpError(403, "The other side has to accept what you proposed.")

        now = timezone.now()
        terms.status = TermsStatus.ACCEPTED
        terms.decided_by = actor.user
        terms.decided_at = now
        terms.save(update_fields=["status", "decided_by", "decided_at"])

        before = contract.status
        if contract.status == ContractStatus.PROPOSED:
            contract.status = ContractStatus.ACTIVE
            contract.activated_at = now
            contract.save(update_fields=["status", "activated_at", "updated_at"])
        audit(
            actor,
            contract,
            ActivityLog.Action.STATUS_CHANGED,
            {
                "status": {"from": before, "to": contract.status},
                "accepted_version": {"from": None, "to": terms.version},
            },
        )
    sync_orders_with_terms(contract)
    generate_orders(contract)
    return contract


@transaction.atomic
def reject_terms(actor: Actor, contract: SupplyContract) -> SupplyContract:
    """Say no. Before signing that ends it; after, the current terms simply stand."""
    contract = _locked(contract)
    terms = pending_terms(contract)
    if terms is None:
        raise HttpError(409, "Nothing is waiting for an answer.")
    if terms.proposed_by_side == actor.side:
        raise HttpError(403, "You proposed this. Propose something else to replace it.")

    terms.status = TermsStatus.REJECTED
    terms.decided_by = actor.user
    terms.decided_at = timezone.now()
    terms.save(update_fields=["status", "decided_by", "decided_at"])

    before = contract.status
    if contract.status == ContractStatus.PROPOSED:
        contract.status = ContractStatus.REJECTED
        contract.save(update_fields=["status", "updated_at"])
    audit(
        actor,
        contract,
        ActivityLog.Action.STATUS_CHANGED,
        {
            "status": {"from": before, "to": contract.status},
            "rejected_version": {"from": None, "to": terms.version},
        },
    )
    return contract


def terminate_contract(
    actor: Actor, contract: SupplyContract, reason: str
) -> SupplyContract:
    """
    Give notice. Deliveries continue until the agreed notice runs out, so neither
    side can drop the other overnight, and anything planned past that is called
    off and refunded.
    """
    with transaction.atomic():
        contract = _locked(contract)
        if contract.status != ContractStatus.ACTIVE:
            raise HttpError(409, "Only an active contract can be terminated.")
        if contract.termination_date is not None:
            raise HttpError(409, "Notice has already been given.")

        terms = latest_accepted(contract)
        contract.termination_date = market_today() + timedelta(days=terms.notice_days)
        contract.terminated_by = actor.side
        contract.termination_reason = reason
        contract.save(
            update_fields=[
                "termination_date",
                "terminated_by",
                "termination_reason",
                "updated_at",
            ]
        )
        contract.versions.filter(status=TermsStatus.PROPOSED).update(
            status=TermsStatus.SUPERSEDED
        )
        audit(
            actor,
            contract,
            ActivityLog.Action.STATUS_CHANGED,
            {"termination_date": {"from": None, "to": str(contract.termination_date)}},
        )
        _close_if_ended(contract)
    sync_orders_with_terms(contract)
    return contract


def _close_if_ended(contract: SupplyContract) -> bool:
    today = market_today()
    if contract.termination_date is not None and contract.termination_date <= today:
        contract.status = ContractStatus.TERMINATED
    elif (terms := latest_accepted(contract)) and terms.end_date < today:
        contract.status = ContractStatus.COMPLETED
    else:
        return False
    contract.save(update_fields=["status", "updated_at"])
    audit(
        SYSTEM,
        contract,
        ActivityLog.Action.STATUS_CHANGED,
        {"status": {"from": ContractStatus.ACTIVE, "to": contract.status}},
    )
    return True


def close_ended_contracts() -> int:
    closed = 0
    for contract in SupplyContract.objects.filter(status=ContractStatus.ACTIVE):
        with transaction.atomic():
            closed += _close_if_ended(_locked(contract))
    return closed


# ── schedule → orders ────────────────────────────────────────────────────────

def terms_for(versions: list[ContractTerms], day: date) -> ContractTerms | None:
    """
    The terms governing `day`: the newest accepted version already in force.

    `versions` is newest first. A later version replaces the earlier ones from
    its start date, end date included, so a shortened contract really is shorter.
    """
    return next((terms for terms in versions if terms.start_date <= day), None)


def terms_on(
    contract: SupplyContract, versions: list[ContractTerms], day: date
) -> ContractTerms | None:
    """The terms of the delivery due on `day`, or None if nothing is due."""
    if contract.termination_date is not None and day > contract.termination_date:
        return None
    terms = terms_for(versions, day)
    if terms is None or day > terms.end_date:
        return None
    if not is_delivery_day(
        terms.delivery_weekdays, terms.every_n_weeks, terms.start_date, day
    ):
        return None
    return terms


def planned_dates(contract: SupplyContract, *, today: date, horizon_days: int):
    """Every (date, terms) of this contract inside the window, in order."""
    versions = list(contract.versions.filter(status=TermsStatus.ACCEPTED))
    for offset in range(horizon_days + 1):
        day = today + timedelta(days=offset)
        terms = terms_on(contract, versions, day)
        if terms is not None:
            yield day, terms


def sync_orders_with_terms(contract: SupplyContract) -> int:
    """
    Bring deliveries already generated into line with what is agreed now.

    Without this a change would miss every order inside the generation horizon:
    the old price would still be charged, and a delivery dropped from the
    schedule could still be confirmed and charged to the buyer.

      * A delivery no longer on the schedule is called off, and refunded if it
        was paid. As the platform rather than either side: both agreed to this,
        so it is nobody's failure to deliver.
      * One the seller has not confirmed takes the current terms.
      * One already confirmed keeps its price. Both sides committed to it.

    Idempotent, because it only compares orders with the current terms. It runs
    after the contract's transaction commits, and `sync_all_contracts` repeats it
    from the job runner, so a crash in between corrects itself. Keeping it
    outside that transaction also means no order is ever locked while the
    contract is, which is the opposite order to settlement and would deadlock.
    """
    versions = list(contract.versions.filter(status=TermsStatus.ACCEPTED))
    changed = 0
    upcoming = contract.orders.filter(
        status__in=UNDISPATCHED, delivery_date__gte=market_today()
    )
    for order in upcoming:
        terms = terms_on(contract, versions, order.delivery_date)
        try:
            if terms is None:
                cancel_order(SYSTEM, order, "No longer on the contract's schedule.")
                changed += 1
            elif order.contract_terms_id != terms.pk:
                changed += apply_contract_terms(order, terms)
        except HttpError:
            # The seller dispatched it a moment ago, so it stands.
            continue
    return changed


def sync_all_contracts(failures: list) -> int:
    changed = 0
    open_order = Q(
        orders__status__in=UNDISPATCHED, orders__delivery_date__gte=market_today()
    )
    contracts = SupplyContract.objects.filter(
        open_order, status__in=[ContractStatus.ACTIVE, ContractStatus.TERMINATED]
    ).distinct()
    for contract in contracts:
        try:
            changed += sync_orders_with_terms(contract)
        except Exception:
            logger.exception("Could not sync contract %s", contract.pk)
            failures.append(contract.pk)
    return changed


def generate_orders(contract: SupplyContract, horizon_days: int | None = None) -> int:
    """
    Create the deliveries coming up. Safe to repeat: `order_one_per_contract_date`
    means a date is only ever generated once, and a delivery cancelled on purpose
    stays cancelled rather than being made again.
    """
    if contract.status != ContractStatus.ACTIVE:
        return 0
    if horizon_days is None:
        horizon_days = settings.CONTRACT_GENERATION_HORIZON_DAYS

    created = 0
    for day, terms in planned_dates(
        contract, today=market_today(), horizon_days=horizon_days
    ):
        if contract.orders.filter(delivery_date=day).exists():
            continue
        try:
            with transaction.atomic():
                order = Order.objects.create(**contract_order_values(contract, terms, day))
                record_generated_order(order)
        except IntegrityError as exc:
            if "order_one_per_contract_date" not in str(exc):
                raise
            continue
        created += 1
    return created


def generate_all_orders(failures: list) -> int:
    total = 0
    active = SupplyContract.objects.filter(status=ContractStatus.ACTIVE).select_related(
        "provider_organization", "customer_organization", "product"
    )
    for contract in active:
        try:
            total += generate_orders(contract)
        except Exception:
            logger.exception("Could not generate orders for contract %s", contract.pk)
            failures.append(contract.pk)
    return total


def side_of(contract: SupplyContract, organization) -> str:
    if contract.provider_organization_id == organization.pk:
        return Side.PROVIDER
    return Side.CUSTOMER

