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

```
agro/           Django project config (settings, root urls, wsgi/asgi)
users/          Custom User model, Organization, Region
api/            REST API endpoints (django-ninja), JWT auth, schemas
```

---

## Feature Roadmap

- [x] JWT authentication (signup, login, token refresh)
- [x] Organization & Region models (B2B tenant structure)
- [x] Signup creates owner + organization in one atomic step
- [ ] Equipment listing (trucks, tractors, harvesters, …)
- [ ] Rental requests & booking flow
- [ ] Staff management (invite users to an organization)
- [ ] Payments & invoicing
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
