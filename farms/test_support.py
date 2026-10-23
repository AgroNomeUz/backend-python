"""
farms/test_support.py
Shared fixtures for the farms tests.

Two organizations, each with an owner, a member who holds no permissions
(reads everything, writes nothing) and a member who holds `farms.manage`. Every
farms test module builds on `FarmsTestCase`, so "the other org" is always there
to check the negative case against.
"""

from django.test import TestCase

from api.auth import create_access_token
from users.models import OrgPermission, Organization, Region, User


def auth(user: User) -> dict:
    """Request kwargs carrying a bearer token for `user`."""
    return {"HTTP_AUTHORIZATION": f"Bearer {create_access_token(user.public_id)}"}


def polygon_geojson(
    lon: float = 69.0, lat: float = 41.0, size: float = 0.001
) -> dict:
    """A small valid square Polygon (GeoJSON, EPSG:4326) with its corner at lon/lat."""
    ring = [
        [lon, lat],
        [lon + size, lat],
        [lon + size, lat + size],
        [lon, lat + size],
        [lon, lat],
    ]
    return {"type": "Polygon", "coordinates": [ring]}


def multipolygon_geojson(**kwargs) -> dict:
    """The same square wrapped as a MultiPolygon."""
    return {
        "type": "MultiPolygon",
        "coordinates": [polygon_geojson(**kwargs)["coordinates"]],
    }


def _make_org(slug: str, region: Region) -> dict:
    owner = User.objects.create_user(
        username=f"{slug}-owner", password="x", full_name=f"{slug} owner"
    )
    org = Organization.objects.create(
        name=f"{slug} org", owner=owner, region=region, is_verified=True
    )
    owner.organization = org
    owner.save(update_fields=["organization"])
    member = User.objects.create_user(
        username=f"{slug}-member",
        password="x",
        organization=org,
        full_name=f"{slug} member",
    )
    manager = User.objects.create_user(
        username=f"{slug}-manager",
        password="x",
        organization=org,
        full_name=f"{slug} manager",
        permissions=[OrgPermission.MANAGE_FARMS.value],
    )
    return {"org": org, "owner": owner, "member": member, "manager": manager}


class FarmsTestCase(TestCase):
    """
    Org A and org B, each with `owner`, `member` (no permissions) and
    `manager` (`farms.manage`): `a_org`, `a_owner`, `a_member`, `a_manager`
    and the same with `b_`.
    """

    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(
            name="Farms Test Region", code="FTR", soato="1799"
        )
        for prefix, slug in (("a", "farm-a"), ("b", "farm-b")):
            for key, value in _make_org(slug, cls.region).items():
                setattr(cls, f"{prefix}_{key}", value)
