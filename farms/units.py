"""
farms/units.py
Allowed unit codes for quantities, and conversion to a common unit.

Quantities are stored as two columns, `<name>` and `<name>_unit`, the unit a
UCUM code. Only area is used so far: hectare (`har`) and square metre (`m2`).
"""

from decimal import Decimal

from django.core.exceptions import ValidationError

HECTARE = "har"
SQUARE_METRE = "m2"

AREA_UNIT_CHOICES = [
    (HECTARE, "Hectare"),
    (SQUARE_METRE, "Square metre"),
]
AREA_UNITS = {code for code, _ in AREA_UNIT_CHOICES}

_M2_PER_HECTARE = Decimal("10000")

# No real field reaches this; it keeps every stored area inside DecimalField(14, 4).
MAX_AREA_HECTARES = Decimal(10) ** 9


def validate_area_unit(value: str) -> None:
    """Field validator: reject any code that is not an allowed area unit."""
    if value not in AREA_UNITS:
        raise ValidationError(
            f"Unknown area unit {value!r}; use one of {sorted(AREA_UNITS)}."
        )


def to_hectares(value: Decimal, unit: str) -> Decimal:
    """Convert an area in `unit` to hectares."""
    validate_area_unit(unit)
    value = Decimal(value)
    if unit == HECTARE:
        return value
    return value / _M2_PER_HECTARE
