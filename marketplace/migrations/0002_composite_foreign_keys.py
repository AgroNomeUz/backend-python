"""
Composite foreign keys, for the same reason as `inquiry_listing_same_org_fk`:
a denormalised organization column is only trustworthy if the database keeps it
in step. Django cannot declare these, so they are raw SQL.

  * An offer can only point at the seller's own crop.
  * An order's seller must be the organization that owns its offer.
  * An order's two parties must be the two parties of its contract.

Each is `NOT DEFERRABLE` and has no ON DELETE action: Django emulates the
`SET_NULL` on `Offer.crop_zone` with an UPDATE, which only clears `crop_zone_id`
and so leaves the pair valid under MATCH SIMPLE.
"""

from django.db import migrations

FOREIGN_KEYS = [
    (
        "marketplace_offer",
        "offer_crop_zone_same_org_fk",
        "(crop_zone_id, organization_id)",
        "farms_cropzone (id, organization_id)",
    ),
    (
        "marketplace_order",
        "order_offer_same_provider_fk",
        "(offer_id, provider_organization_id)",
        "marketplace_offer (id, organization_id)",
    ),
    (
        "marketplace_order",
        "order_contract_same_provider_fk",
        "(contract_id, provider_organization_id)",
        "marketplace_supplycontract (id, provider_organization_id)",
    ),
    (
        "marketplace_order",
        "order_contract_same_customer_fk",
        "(contract_id, customer_organization_id)",
        "marketplace_supplycontract (id, customer_organization_id)",
    ),
]


class Migration(migrations.Migration):

    dependencies = [
        ("marketplace", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=f"""
                ALTER TABLE {table}
                  ADD CONSTRAINT {name}
                  FOREIGN KEY {columns}
                  REFERENCES {target}
                  NOT DEFERRABLE;
            """,
            reverse_sql=f"ALTER TABLE {table} DROP CONSTRAINT {name};",
        )
        for table, name, columns, target in FOREIGN_KEYS
    ]
