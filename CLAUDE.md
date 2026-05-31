# Agro Backend — Agent Ruleset

Rules every agent (AI or human) working on this project **must** follow.

---

## Rule 1 — Document the session

At the end of every session append an entry to `SESSIONS.md`:

```markdown
## YYYY-MM-DD — <branch name>
- **Summary**: what was done
- **Files changed**: list
- **How to verify**: migration commands, endpoints to call, etc.
```

## Rule 2 — Branch before working

**Never commit directly to `main`.**

Before touching any code:
```bash
git checkout main && git pull
git checkout -b feature/<short-description>
```

Merge back via PR after review.

## Rule 3 — Maintain the project description

`README.md` is the living description of the project.
- Create it if it doesn't exist.
- Append/update it whenever a new major feature or app is added.
- Keep the **Tech Stack**, **App Layout**, and **Feature Roadmap** sections current.

---

## Project shape (quick reference for agents)

| Concern           | Location                        |
|-------------------|---------------------------------|
| Django settings   | `agro/settings.py`              |
| URL root          | `agro/urls.py`                  |
| Custom User model | `users/models.py` → `User`      |
| Auth JWT          | `api/auth.py`                   |
| API endpoints     | `api/views.py` (django-ninja)   |
| API schemas       | `api/schemas.py` (Pydantic/Ninja) |
| Postgres via docker | `docker-compose.yml` + `.env` |

- **Framework**: Django 5 + django-ninja (FastAPI-style, async-capable)
- **Auth**: JWT access (30 min) + refresh (30 days) tokens, stored in `api.RefreshToken`
- **DB**: PostgreSQL 16 (docker-compose in dev)
- **Custom User**: `users.User` (AbstractUser), set in `AUTH_USER_MODEL`
- **B2B-first model**: all users belong to an `Organization`; the first user of an org is its **owner**; staff are added later.
