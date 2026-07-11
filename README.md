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
- [ ] Equipment listing API (trucks, tractors, harvesters, …)
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
