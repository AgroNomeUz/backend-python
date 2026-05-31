# Session Log

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

# 4. POST /api/auth/signup
curl -X POST http://localhost:8000/api/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{
    "username": "farmowner",
    "email": "farm@example.com",
    "password": "s3cr3t",
    "first_name": "Ali",
    "last_name": "Karimov",
    "organization_name": "GreenField LLC",
    "region_code": "TSH",
    "organization_address": "123 Field Road",
    "tax_number": "123456789"
  }'
# Expected: 200 with access_token, refresh_token, user (with nested organization)

# 5. Re-signup with same username → 400 "Username already taken"
```
