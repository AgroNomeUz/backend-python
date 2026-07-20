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

## 2026-07-11 — equipment
- **Summary**: Architecture discussion — decided to keep a modular monolith
  (no microservices) with one Django app per bounded context. Confirmed
  WorkOrder/WorkSession stay (operator services with per-hectare billing are
  in MVP scope). Documented current + planned app layout (`farms/`,
  `marketplace/`, `billing/`, `notifications/`, `telemetry/`) and service-layer
  conventions in README.md; refreshed the feature roadmap. Also flagged (not
  yet applied): `Booking.customer` should become `customer_organization` FK +
  `created_by` user FK, since bookings are org-to-org.
- **Files changed**: README.md, SESSIONS.md
- **How to verify**: read README.md "App Layout" and "Feature Roadmap"
  sections; no code/migration changes.

## 2026-07-11 — equipment (migration readiness)
- **Summary**: Prepared the first farms + equipment migration. Fixed four
  blockers: (1) registered the new `farms` app in INSTALLED_APPS; (2) moved
  Farm/Field/CropSeason admin classes from equipment/admin.py to
  farms/admin.py (equipment/admin.py imported CropSeason which is no longer
  importable from equipment.models — startup crash); (3) switched the
  docker-compose db image from postgres:16 to postgis/postgis:16-3.5 (settings
  use the postgis backend; plain postgres cannot CREATE EXTENSION postgis);
  (4) fixed CONN_MAX_AGE — settings read DB_CONN_MAX_AGE but .env defines
  CONN_MAX_AGE, and the value is now cast to int. Generated initial
  migrations: farms/0001, equipment/0001 + 0002 (Django split the cross-app
  FKs to farms into 0002 with an explicit dependency). `manage.py check`
  passes.
- **Files changed**: agro/settings.py, docker-compose.yml, equipment/admin.py,
  farms/admin.py, farms/migrations/0001_initial.py,
  equipment/migrations/0001_initial.py, equipment/migrations/0002_initial.py,
  SESSIONS.md
- **How to verify**:
  ```bash
  docker compose down          # db image changed — recreate the container
  docker compose up --build -d
  docker compose run --rm web python manage.py migrate
  # expect: users, farms, equipment migrations apply cleanly
  ```
  If the old postgres_data volume misbehaves with the new image (dev data
  only): `docker compose down -v` and re-run.

## 2026-07-11 — equipment (public UUID ids)
- **Summary**: Added `public_id` UUID (non-primary, unique) to every domain
  model via a new abstract base `core.models.PublicIdModel`; integer PKs stay
  internal. Applied to users (User, Region, Organization), farms (3 models)
  and equipment (19 models). api.RefreshToken was deliberately skipped — it is
  never addressed by id (the token string is the lookup key). Migrations are
  hand-written in three phases (add nullable → backfill distinct UUIDs →
  alter to unique) so they are safe on populated tables. API now exposes
  UUIDs: schemas' `id` fields are UUID (OrganizationOut.region is now properly
  nullable instead of the fake id-0 region), views pass `public_id`, and JWT
  access/refresh tokens carry `str(user.public_id)` in the `user_id` claim
  with lookup by `public_id` — existing dev tokens are invalidated.
- **Files changed**: core/__init__.py, core/models.py (new), users/models.py,
  farms/models.py, equipment/models.py, users/migrations/0004_public_id.py,
  farms/migrations/0002_public_id.py, equipment/migrations/0003_public_id.py,
  api/schemas.py, api/views.py, api/auth.py, README.md, SESSIONS.md
- **How to verify**:
  ```bash
  docker compose run --rm web python manage.py migrate
  # login again (old JWTs are invalid), then check /api/docs:
  # signup/login responses must show uuid ids for user/organization/region
  ```

## 2026-07-11 — equipment (hitch category on EquipmentModel)
- **Summary**: Added `hitch_category` to EquipmentModel — the hitch a tractor
  PROVIDES — so implement requirements (EquipmentModelCompatibility) can be
  matched against tractors generically. Promoted HitchCategory from a nested
  class on EquipmentModelCompatibility to module level, shared by both models
  (same choice values, so no migration needed for the compatibility table).
  Decision: tractor size class (small/midsize/large) is NOT stored — it is
  derived from engine_power_kw power bands at the query/API layer.
- **Files changed**: equipment/models.py, equipment/admin.py,
  equipment/migrations/0004_equipmentmodel_hitch_category.py, SESSIONS.md
- **How to verify**:
  ```bash
  docker compose run --rm web python manage.py migrate
  # admin → Equipment models: hitch_category column + filter present
  ```

## 2026-07-20 — equipment (catalog + asset API, activity log)
- **Summary**: Exposed the first org-facing write API. Catalog endpoints
  (manufacturers, category tree, equipment models) are read-only shared
  reference data and feed the frontend's "pick your machine" flow; the model
  list supports search, category (slug, includes subcategories), manufacturer,
  power range, `size_class`, hitch and production-year filters. Assets are
  CRUD, always scoped to `request.auth.organization` — a foreign asset id 404s
  rather than leaking existence. Every asset write records a `core.ActivityLog`
  row (actor, field-level diff, request ip/UA/path); a status-only edit is
  logged as `status_changed`, and the DELETE row is written before the delete
  so history outlives the object. Activity is readable per-asset and org-wide.
  Assets referenced by bookings/work sessions return 409 on delete (retire
  instead). Endpoints are sync — django-ninja bridges the async JWTBearer.
- **Files changed**: equipment/views.py, api/views.py (router wiring),
  core/migrations/0001_initial.py (new, ActivityLog), SESSIONS.md
- **How to verify**:
  ```bash
  docker compose exec web python manage.py migrate
  # /api/v1/docs → Catalog, Assets, Activity sections (all need a bearer token)
  # POST /api/v1/auth/login, then:
  #   GET  /api/v1/catalog/categories
  #   GET  /api/v1/catalog/equipment-models?category=tractor&size_class=large
  #   POST /api/v1/assets  {"equipment_model_id": "<uuid>", "serial_number": "SN-1"}
  #   PATCH/DELETE /api/v1/assets/{id}
  #   GET  /api/v1/assets/{id}/activity   and   GET /api/v1/activity
  ```
