# TapTap Django

A Django + Bootstrap + PostgreSQL + Docker rebuild of the TapTap Wi‑Fi hotspot management platform.

## Included
- Business registration/login and 7-day trial
- Subscription plans and access gate
- Dashboard with KPI cards and quick actions
- Voucher generation, listing, disable, delete-expired and MAC reset
- Batch voucher generation/history
- MikroTik router records, connection test, active users and disconnect
- **Two-way MikroTik synchronization**
  - Pull RouterOS hotspot users and IP bindings into TapTap
  - Push TapTap-assigned vouchers and TapTap-created IP bindings to RouterOS
  - MikroTik-only hotspot users stay separate from commercial TapTap vouchers
  - Per-router Sync and Sync All controls
- **Live network topology**
  - RouterOS MNDP/CDP/LLDP neighbor discovery
  - Online/offline remembered routers and access points
  - Router-to-router link detection for configured TapTap routers
  - Physical MikroTik Ethernet/SFP front-panel visualization
  - Per-port running/disabled state
  - Neighbor identity and learned client information per port
  - Bridge-host, DHCP, ARP and Wi-Fi registration correlation
  - CAPsMAN remote AP discovery where supported
- IP bindings (add/toggle/delete + database mirror)
- Plans, reports, finance summary and security pages
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

The Docker entrypoint runs Django migrations automatically, including the topology, control-center and background-sync tables through migration `0004_router_sync_jobs`.

## MikroTik discovery requirements
RouterOS API must be enabled and reachable from the Docker host. For the clearest topology, keep Neighbor Discovery enabled on the LAN/bridge interfaces that should participate in MNDP/LLDP/CDP. Bridge learning is used to associate learned MAC addresses with physical interfaces. Wi-Fi registration and CAPsMAN data are read when those RouterOS menus are available.

Store router credentials only in the app database and use a dedicated least-privilege RouterOS API user where possible.

## v3 — MikroTik Control Center

This build adds deeper RouterOS synchronization and management:

- Full Sync imports `/ip/hotspot/user/profile` into TapTap Plans and `/ip/hotspot/user` into TapTap Vouchers.
- Plan names and voucher codes are matched before creation so an existing database record is not duplicated.
- MikroTik-imported plans keep price `D0` because RouterOS user profiles do not contain a sales price; set the commercial price in TapTap as needed.
- Unified MAC/device inventory merges bridge hosts, ARP, DHCP leases, HotSpot host/active records, wireless registrations and Neighbor Discovery.
- Live topology includes physical Ethernet/SFP port state, learned devices, upstream AP/router/switch when discovery can identify it, and remembered offline neighbors.
- Router Control Center provides a drag/drop interface role studio, bridge selection, port enable/disable, a Quick WAN DHCP + NAT recipe, a broad redacted configuration snapshot, a resource browser, and an audited advanced add/set/remove editor.
- Load-balancing analysis detects PCC/mangle marking, ECMP/equal-cost paths, failover route distances and bonding. The Control Center polls live interface byte counters to animate the detected WAN paths.

### Database migration

Docker runs migrations automatically from `entrypoint.sh`. After replacing an older version you can run explicitly:

```bash
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py showmigrations core
```

You should see `0003_mikrotik_control_center` marked `[X]`.

### Important RouterOS notes

The TapTap server must be able to reach the RouterOS API port configured for each router. For best topology results, enable Neighbor Discovery (MNDP/LLDP/CDP) on the LAN/bridge interfaces you want to map. Advanced changes can affect connectivity; TapTap requires an explicit `APPLY` confirmation and records every advanced change in the audit table.


## v4 — Background sync + scrollable data panels

- Router synchronization now runs in a dedicated Celery worker backed by Redis. The web request only queues the job, so you can continue using TapTap or leave the Routers page while a scan runs.
- Sync All queues one job per router and prevents a second queued/running job for the same router.
- The Routers page polls persistent job records and shows queued/running/completed/failed state, percentage and current phase.
- Sync progress covers RouterOS connection, plan import, voucher reconciliation, IP bindings, MAC/port/neighbor discovery, topology and configuration snapshot.
- Long tables and operational lists are vertically scrollable with sticky table headers. This applies to voucher, plan, user, IP-binding, topology/device, report, finance and inventory tables, plus router lists and change logs.
- Docker Compose now includes a persistent Redis service and a separate `worker` service.

After upgrading, rebuild and apply migration `0004_router_sync_jobs`:

```bash
docker compose down
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py showmigrations core
docker compose ps
```

`docker compose ps` should show `web`, `worker`, `db` and `redis` running.
