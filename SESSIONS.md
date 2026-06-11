# Session Log

---

## 2026-06-11 — equipment

### Summary
Designed and implemented the full MVP model layer for the agricultural equipment rental platform. No migrations generated — models are ready for review first.

### What was done
- **Rewrote `equipment/models.py`** — complete MVP schema (21 models across 9 domains):
  - Catalog: `Manufacturer`, `EquipmentCategory`, `EquipmentModel`, `EquipmentModelCompatibility`
  - Assets: `Asset` (physical machine with PostGIS `PointField` for location)
  - Farms: `Farm` (PointField), `Field` (MultiPolygonField), `CropSeason`
  - Pricing: `AvailabilityPeriod`, `PricingRule`, `DepositRule`
  - Bookings: `Booking` (with `ALLOWED_TRANSITIONS` map), `BookingItem`, `BookingStatusHistory`
  - Work: `WorkOrder`, `WorkSession` (MultiLineStringField for GPS track)
  - Maintenance: `MaintenanceRecord`, `Inspection`, `FaultReport`
  - Documents: `Document` (generic FK via ContentType)
  - Events/Ext: `AssetEvent`, `ExternalReference` (generic FK adapter table for ADAPT/ISOBUS/JD/CLAAS)
- **Updated `agro/settings.py`**:
  - Added `django.contrib.gis` to INSTALLED_APPS
  - Added `equipment.apps.EquipmentConfig` to INSTALLED_APPS
  - Changed DB engine to `django.contrib.gis.db.backends.postgis`
- **Wrote `equipment/admin.py`** — all 21 models registered with GISModelAdmin where needed

### Files changed
- `equipment/models.py` *(full rewrite)*
- `equipment/admin.py` *(full rewrite)*
- `agro/settings.py` *(added gis + equipment app, changed DB engine)*

### How to verify (after review and migration approval)
```bash
# 1. Install PostGIS in docker (already in docker-compose if using postgis image)
# 2. Generate migrations
python manage.py makemigrations equipment

# 3. Check for errors
python manage.py check

# 4. Apply
python manage.py migrate

# 5. Smoke-test via shell
python manage.py shell -c "
from equipment.models import Manufacturer, EquipmentCategory, EquipmentModel
m = Manufacturer.objects.create(name='John Deere', country='US')
cat = EquipmentCategory.objects.create(name='Tractor', slug='tractor', is_self_propelled=True)
em = EquipmentModel.objects.create(manufacturer=m, category=cat, name='8R 410', engine_power_kw=302, is_self_propelled=True)
print(em)
"
```

---

## 2026-05-31 — feature/users-organization-signup (bug fixes, committed by user)

### Summary
Nullable-region serialization bugs introduced when `Organization.region` was made optional.

### What was fixed
- **`api/schemas.py` — `RegionOut.code`**: changed type from `str` to `str | None` so Pydantic/Ninja correctly serializes a region with no code (or a null region placeholder).
- **`api/views.py` — `_issue_tokens` region dict**: added null-guards for the case where `org.region` is `None` (newly nullable after simplification):
  - `"id"` → `org_obj.region_id or 0` (avoids `None` where an int is expected)
  - `"name"` → `org_obj.region.name if org_obj.region else ""`
  - `"code"` → `org_obj.region.code if org_obj.region else None`

### Root cause
Making `Organization.region` nullable (migration 0003) meant the serialization path in `_issue_tokens` could receive `None` for the region FK and its fields, crashing with attribute errors. The schema also expected a non-nullable `str` for `code`.

### Files changed
- `api/schemas.py`
- `api/views.py`

---

## 2026-05-31 — feature/users-organization-signup

### Summary
Initial org layer and project ruleset setup.

### What was done
- Created `CLAUDE.md` — agent ruleset (branch, document, README rules + project shape reference)
- Created `README.md` — full project description (overview, stack, app layout, roadmap, API table, org model diagram)
- Extended `users/models.py`:
  - `Region` — name + code (≤15 chars), ordered by name
  - `Organization` — name, address, region FK, owner OneToOne(User), tax_number, phone, email, created_at, updated_at
  - `User` — added `organization` FK (members), `is_organization_owner` property
- Updated `users/admin.py` — registered `User`, `Region`, `Organization` with search/filter/list_display
- Generated and applied `users/migrations/0002_region_organization_user_organization.py`
- Updated `api/schemas.py` — `RegionOut`, `OrganizationOut`, updated `UserOut` (org nested), `SignUpIn` (org fields)
- Updated `api/views.py` — `signup` endpoint now atomically creates User + Organization in a transaction; the user becomes owner and member simultaneously; `_issue_tokens` now returns org data in the response

### Files changed
- `users/models.py`
- `users/admin.py`
- `users/migrations/0002_region_organization_user_organization.py` *(generated)*
- `api/schemas.py`
- `api/views.py`
- `CLAUDE.md` *(new)*
- `README.md` *(new)*
- `SESSIONS.md` *(new — this file)*

### How to verify
```bash
# 1. Migrations
docker compose run --rm web python manage.py migrate
docker compose run --rm web python manage.py check

# 2. Start dev server
docker compose up

# 3. Create a region via admin or shell
docker compose run --rm web python manage.py shell -c "
from users.models import Region
Region.objects.get_or_create(name='Tashkent', code='TSH')
"

# 4. POST /api/auth/signup (only credentials required; org created empty)
curl -X POST http://localhost:8000/api/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{
    "username": "farmowner",
    "email": "farm@example.com",
    "password": "s3cr3t"
  }'
# Expected: 200 with access_token, refresh_token, user (with nested empty organization)

# 5. Re-signup with same username → 400 "Username already taken"
```
