# TapTap Django

A Django + Bootstrap + Docker rebuild of the TapTap Wi-Fi hotspot management platform.

## Included
- Business registration/login and 7-day trial
- Subscription plans and access gate
- Dashboard with KPI cards and quick actions
- Voucher generation, listing, disable, delete-expired and MAC reset
- Batch voucher generation/history
- MikroTik router records, connection test, active users and disconnect
- IP bindings (add/toggle/delete)
- Plans, reports, finance summary, topology and security pages
- Responsive Bootstrap UI with mobile off-canvas navigation
- PostgreSQL + Docker Compose production baseline

## Quick start
```powershell
Copy-Item .env.example .env
docker compose up --build
```
Open http://localhost:8000

Create an admin account:
```powershell
docker compose exec web python manage.py createsuperuser
```

## Notes
MikroTik RouterOS API must be enabled and reachable from the Docker host. Store router credentials only in the app database and use a dedicated least-privilege RouterOS user where possible.
