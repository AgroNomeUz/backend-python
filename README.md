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

- [x] JWT authentication (login, token refresh, logout)
- [x] Phone + OTP login and organization signup (SMS mocked to the log)
- [x] Organization & Region models (B2B tenant structure)
- [x] Signup creates owner + organization in one atomic step
- [x] Equipment domain models (catalog, assets, pricing, bookings, work orders)
- [x] Equipment catalog API (models picker: manufacturers, categories, specs)
- [x] Org asset registry API (add/edit/remove machines, org-scoped)
- [x] Activity log — who changed what inside an organization
- [x] Staff management — add employees to an organization, per-member permissions
- [ ] Rental requests & booking flow
- [ ] Work orders: operator services billed per hectare / shift / operation
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

| Endpoint                       | Method | Auth  | Description                        |
|--------------------------------|--------|-------|------------------------------------|
| `/api/v1/auth/otp/request`     | POST   | None  | Send a login code to a phone; says whether the number is known |
| `/api/v1/auth/otp/verify`      | POST   | None  | Code → tokens, or a `signup_token` if nobody owns the number |
| `/api/v1/auth/org/signup`      | POST   | None  | `signup_token` → new organization + its admin. The only public account-creating endpoint |
| `/api/v1/auth/login`           | POST   | None  | Password login (phone, email or username) |
| `/api/v1/auth/token/refresh`   | POST   | None  | Rotate JWT refresh token           |
| `/api/v1/auth/logout`          | POST   | JWT   | Revoke the presented refresh token |
| `/api/v1/auth/password/change` | POST   | JWT   | Set your own password; clears `must_change_password` |

**Phone + OTP is the front door**; email + password is kept for admins and
back-office, and is how an employee uses the one-time password their admin
gave them. There is no open self-registration: an unknown phone number is
never turned into an account by itself — it gets a short-lived, single-use
`signup_token` that only `POST /auth/org/signup` accepts.

There is no SMS gateway yet — `api/sms.py` is the seam, and it delivers
nothing. A passcode is a bearer credential (anyone holding it can exchange it
for that account's tokens at the public verify endpoint), so it is never
written to the log and never returned to the caller. **Message bodies are not
logged at all**, and the number in the delivery report is masked.

To actually sign in while the gateway is mocked, pick one — both are off by
default and both refuse to run when `ENVIRONMENT=production`:

| Setting | Effect |
|---------|--------|
| `OTP_TEST_PHONES=+998901234567,…` | Those numbers get the fixed `OTP_DEV_CODE` (default `000000`), returned in the response as `debug_code`. Only ever list numbers you control |
| `OTP_ECHO_CODES=true` | Writes real codes for **any** number to the log. Convenient and dangerous: anyone who can read the logs can take over any account |

The limits around it are real — a 60s resend cooldown, hourly caps per number
and per IP, and 5 attempts per code, all counted off the `api.PhoneOtp` table
(tunable in `settings.py`).

Every auth payload carries the caller's effective `permissions`, `is_owner`
and `must_change_password` so the frontend can route a freshly-registered
employee straight to a password screen.

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

### Members — the caller's organization's staff

| Endpoint                              | Method | Permission      | Description                       |
|---------------------------------------|--------|-----------------|-----------------------------------|
| `/api/v1/members/me`                  | GET    | —               | The caller's own record + permissions |
| `/api/v1/members`                     | GET    | —               | Roster (`?search=`, `?is_active=`, `?permission=`) |
| `/api/v1/members`                     | POST   | `users.manage`  | Add an employee by email          |
| `/api/v1/members/{id}`                | GET    | —               | One member                        |
| `/api/v1/members/{id}`                | PATCH  | `users.manage`  | Names, `permissions`, `is_active` |
| `/api/v1/members/{id}`                | DELETE | `users.manage`  | Deactivate + revoke their sessions|
| `/api/v1/members/{id}/reset-password` | POST   | `users.manage`  | Issue a fresh one-time password   |

`POST /members` takes an email (plus an optional phone, names and permissions) and
returns the new member **together with a one-time `temporary_password`, shown
only in that response**. The employee logs in with that email and password,
arrives with `must_change_password: true`, and clears it via
`POST /auth/password/change` — which also revokes every outstanding refresh
token. Losing the one-time password is recoverable via `reset-password`.

Guard rails: the owner cannot be deactivated, demoted, password-reset or
**have their phone changed** by their own staff, and nobody can change their
own permissions or deactivate themselves — that has to come from another
admin. The phone rule matters because phone is the OTP login identifier:
without it, anyone holding `users.manage` could point the owner's number at a
handset they control and sign in as the owner.

Note this router is for administering *other* people, which is why every write
here needs `users.manage`. A member editing their **own** profile uses
`/api/v1/users/me` below, which needs no permission at all.

### Your own profile and organization

| Endpoint                        | Method | Permission     | Description                          |
|---------------------------------|--------|----------------|--------------------------------------|
| `/api/v1/users/me`              | GET    | —              | Your profile with the organization nested, plus `is_owner` + `permissions` |
| `/api/v1/users/me`              | PATCH  | —              | Your own `full_name`, `telegram`, `email` |
| `/api/v1/users/me/phone`        | POST   | —              | Start a phone change: sends a code to the **new** number |
| `/api/v1/users/me/phone/verify` | POST   | —              | `{phone, code}` → the swap           |
| `/api/v1/org`                   | GET    | —              | The org profile + `member_count` and `created_at` |
| `/api/v1/org`                   | PATCH  | `users.manage` | Name, address, region, tax number, phone, email |

`GET /users/me` returns exactly the `user` object that every auth response
carries — the same builder produces both — so a client can cache what login
gave it and refresh from here without the two drifting.

`GET /users/me` supports the no-org account (`organization: null`), but
`PATCH /users/me` 403s for the same account: every write is audited (§0.3)
and `core.ActivityLog.organization` is non-nullable, so there is no
organization to hang the audit row on. This is a deliberate asymmetry
between the two, not an oversight.

**Changing a phone costs a code sent to the new number**, because phone is the
login identifier: an unverified swap on a typo locks the account out, and a
deliberate one is a takeover. The new number is checked to be free *before* a
code is spent on it, and the swap is audited. `PATCH /users/me` therefore has
no `phone` field.

`email`, by contrast, is accepted unverified — there is no mail gateway in this
project, and an email signs you in only *with* a password, unlike a phone
number, which is a session on an OTP alone. Anything that later makes email
sufficient on its own (a password reset by email) has to bring a verification
step with it.

`PATCH /org` cannot write `is_verified` or `entity_type` at any value: they are
not fields of the input schema. Verification is a statement the platform makes
about an organization, not one it makes about itself.

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
- Non-owner staff are added via `POST /api/v1/members` by anyone holding
  `users.manage`.
- `user.is_organization_owner` → `True` if the user owns the org.

### Permissions

Org-scoped permissions live in `users.OrgPermission` and are stored per user
in `User.permissions` (a Postgres array of codes). They are entirely separate
from Django's own `auth.Permission` machinery, which stays for the admin site.

| Code               | Gates                                        |
|--------------------|----------------------------------------------|
| `equipment.manage` | POST/PATCH/DELETE on `/api/v1/assets`        |
| `users.manage`     | Every write under `/api/v1/members`          |

- **Reads are open** to any member of the organization; a permission only ever
  gates a write.
- **The owner implicitly holds every code** (`User.has_org_perm`) and is never
  stored with permissions, so an organization can't lock itself out. The owner
  reads back the full set from `org_permissions`.
- Permissions are read from the database on every request, never from the JWT,
  so a grant or revoke takes effect on the member's next call — no re-login.
- Gate a new write endpoint with
  `users.permissions.require_perm(request, OrgPermission.X)`; add a code to
  `OrgPermission` when a new manageable area ships.
