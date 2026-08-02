# Agro Backend

A B2B marketplace platform for the **agricultural sphere**, starting with
equipment rentals (trucks, tractors, combine harvesters, and other machinery),
with more features to follow.

## Segments

The platform is designed to support **B2B**, **B2C**, and **P2P** interactions.
To keep the database models simple and maintainable, it is modelled as a
**B2B-only** platform internally — some capabilities are selectively enabled or
hidden for non-B2B users.

---

## Tech Stack

| Layer           | Technology                          |
|-----------------|-------------------------------------|
| Language        | Python 3.14                         |
| Framework       | Django 5                            |
| API             | django-ninja (OpenAPI, async-ready) |
| Auth            | JWT (PyJWT) — access + refresh flow |
| Database        | PostgreSQL 16                       |
| Containerisation| Docker + docker-compose             |

---

## App Layout

The project is a **modular monolith**: one Django project, one PostgreSQL
database, with each bounded context isolated in its own Django app. Apps talk
to each other through service-layer functions (`<app>/services.py`), never by
reaching into another app's models from views. This keeps a future extraction
into separate services mechanical, if scale ever demands it.

### Current apps

```
agro/           Django project config (settings, root urls, wsgi/asgi)
core/           Shared bases: PublicIdModel (UUID public ids), ActivityLog audit
users/          Identity & tenancy: custom User, Organization, Region
api/            HTTP layer: django-ninja routers, JWT auth, Pydantic schemas
equipment/      Equipment domain: catalog (manufacturers, models, compatibility),
                assets, pricing & availability, bookings, work orders & sessions,
                maintenance & inspections, documents, asset events
```

### Planned apps (bounded contexts)

```
farms/          Farm, Field, CropSeason — extracted from equipment/ so that
                marketplace and agronomy features can share them
marketplace/    Goods listing & orders (seeds, fertilizer, produce, parts)
billing/        Invoices, payments, deposits — one money ledger for all contexts
notifications/  SMS / push / email fan-out (backed by a task queue)
telemetry/      High-volume GPS / ISOBUS / manufacturer-API ingestion.
                First candidate for extraction into a real service: write-heavy,
                loosely coupled, no transactional ties to the core schema
```

### Conventions

- **Public IDs are UUIDs**: every domain model inherits `core.models.PublicIdModel`,
  which adds a unique `public_id` UUID alongside the integer PK. The API only
  ever exposes and accepts `public_id`; integer PKs never leave the backend
  (they stay for cheap joins). Look up API-supplied ids with
  `Model.objects.get(public_id=...)`.
- One django-ninja **router per app**, mounted under a namespace
  (`/api/auth/…`, `/api/equipment/…`, `/api/market/…`) — the API is already
  shaped like service boundaries without the distributed-systems cost.
- **Cross-app writes go through services**, e.g.
  `equipment.services.confirm_booking()` — views and other apps never mutate
  foreign models directly. Status transitions (`Booking.ALLOWED_TRANSITIONS`)
  are enforced there.
- **Async work** (notifications, telemetry ingestion) goes to a task queue
  (Celery/Dramatiq), not into request handlers.

---

## Feature Roadmap

- [x] JWT authentication (signup, login, token refresh)
- [x] Organization & Region models (B2B tenant structure)
- [x] Signup creates owner + organization in one atomic step
- [x] Equipment domain models (catalog, assets, pricing, bookings, work orders)
- [x] Equipment catalog API (models picker: manufacturers, categories, specs)
- [x] Org asset registry API (add/edit/remove machines, org-scoped)
- [x] Activity log — who changed what inside an organization
- [ ] Rental requests & booking flow
- [ ] Work orders: operator services billed per hectare / shift / operation
- [ ] Staff management (invite users to an organization)
- [ ] Farm management app (`farms/`: extract Farm, Field, CropSeason)
- [ ] Marketplace for agro goods (`marketplace/`)
- [ ] Payments & invoicing (`billing/`)
- [ ] Notifications (SMS / push)
- [ ] Telemetry ingestion (GPS, ISOBUS) — post-MVP
- [ ] Reviews & ratings

---

## Local Development

### Prerequisites
- Docker & docker-compose
- A `.env` file at the project root (see `.env.example` when created)

### Start services

```bash
docker-compose up --build
```

The Django dev server starts on **http://localhost:8000**.  
Interactive API docs: **http://localhost:8000/api/docs**

### Run migrations manually (inside container or venv)

```bash
python manage.py migrate
```

### Create a superuser

```bash
python manage.py createsuperuser
```

---

## API Overview

| Endpoint                    | Method | Auth  | Description                        |
|-----------------------------|--------|-------|------------------------------------|
| `/api/auth/signup`          | POST   | None  | Register user + create organization|
| `/api/auth/login`           | POST   | None  | Login (username or email)          |
| `/api/auth/token/refresh`   | POST   | None  | Rotate JWT refresh token           |

### Catalog — shared reference data, read-only

| Endpoint                                | Method | Description                              |
|-----------------------------------------|--------|------------------------------------------|
| `/api/v1/catalog/manufacturers`         | GET    | Brands (`?search=`)                      |
| `/api/v1/catalog/categories`            | GET    | Category tree, children inlined          |
| `/api/v1/catalog/categories/{slug}`     | GET    | One category                             |
| `/api/v1/catalog/equipment-models`      | GET    | Models the user picks from (see filters) |
| `/api/v1/catalog/equipment-models/{id}` | GET    | One model with full specs                |

Model filters: `search`, `manufacturer_id`, `category` (slug — a top-level slug
also matches its subcategories), `is_self_propelled`, `hitch_category`,
`min_power_kw`, `max_power_kw`, `size_class` (`small`/`midsize`/`large`),
`year`. Catalog rows are populated by admins, never by API clients.

### Assets — owned by the caller's organization

| Endpoint                        | Method | Description                               |
|---------------------------------|--------|-------------------------------------------|
| `/api/v1/assets`                | GET    | The org's machines (`?search=`, `?category=`, `?operational_status=`, `?ownership_status=`, `?bookable=`) |
| `/api/v1/assets`                | POST   | Register a machine into the org           |
| `/api/v1/assets/{id}`           | GET    | One asset                                 |
| `/api/v1/assets/{id}`           | PATCH  | Partial update (only keys sent are applied)|
| `/api/v1/assets/{id}`           | DELETE | Remove; 409 if booked — retire instead    |
| `/api/v1/assets/{id}/activity`  | GET    | That asset's full history                 |
| `/api/v1/activity`              | GET    | Org-wide change history (`?action=`, `?target_type=`) |

Assets are scoped to `request.auth.organization`: another org's id returns
**404**, not 403, so ids can't be probed. All list endpoints are paginated
(`?limit=` / `?offset=`, returning `{"items": [...], "count": N}`).

### Audit trail

Every write to org-owned data appends a `core.ActivityLog` row — actor, action,
generic-FK target, `target_repr` snapshot, a field-level `changes` diff, and
request context (ip, user agent, method, path). Rows are append-only and
survive deletion of their target. Status-only edits are recorded as
`status_changed` rather than `updated`. New write endpoints must log here too;
use `ActivityLog.record()` with the helpers in `core/audit.py`.

Full interactive docs at `/api/docs` (Swagger UI via django-ninja).

---

## Organization & Ownership Model

```
Region ──< Organization ──< User (members)
                │
                └─── owner (OneToOne → User)
```

- Every **Organization** has one **owner** (the user who signed up).
- All users, including the owner, are **members** of exactly one organization.
- Non-owner staff are added to the organization by the owner (future feature).
- `user.is_organization_owner` → `True` if the user owns the org.
