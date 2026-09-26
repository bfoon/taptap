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


## v5 — Live topology map, load-balancing engine, Security Center, deploy anywhere

### Live network topology
- One map for the whole business: Internet → ISP modem → WAN links → managed MikroTiks → switches / access points → device groups.
- Managed routers are linked by MAC, IP **or** identity, so a router added by its public or VPN address still connects to its neighbours. A router whose WAN is fed by another managed router hangs under it instead of showing a second Internet path.
- Traffic particles move along every link in real time (download cyan, upload violet); speed and density follow real bits/second from RouterOS `monitor-traffic`.
- Pan, zoom, fit, full screen, search by name / MAC / IP (including devices inside groups), device drawer with details, "Discover now" per router.
- The page opens instantly from the database. Discovery runs per router in parallel, each bounded by `MIKROTIK_TIMEOUT`, so one dead router never blocks the page — it shows why it is unreachable (for example a private IP without a VPN).

### Load-balancing engine
- WAN detection rewritten for RouterOS v6 and v7: gateways as IP, interface or `ip%interface`, `gateway-status`, `immediate-gw`, DHCP-client and PPPoE routes, recursive/ECMP routes, PCC → connection-mark → routing-mark → table chains, routing rules, bonding and operator-declared WAN ports.
- Methods: PCC, policy routing, ECMP, failover, bonding, single WAN — with planned share per link, active/standby/down state and warnings (HotSpot + policy routing, failover without check-gateway, PCC marks into tables without a default route).
- Live view: animated lanes, per-link down/up rates, share-of-traffic vs plan, sparkline, balance meter, and a failover event log ("ether1 went down — traffic moved to ether2").
- Telemetry polls are cheap (default routes + WAN counters only) and cached in Redis, so many open tabs cost one router read.

### Security Center
- Grade and score per business and per router, filters by severity and category, and "Mark as reviewed" with a note (migration `0005_security_ack`).
- Router hardening: missing WAN input firewall, Telnet/FTP, services open to any address, open DNS resolver, SOCKS / web proxy / UPnP / bandwidth server, SNMP `public`, MAC-Winbox on all interfaces, discovery on all interfaces, default `admin` user, TapTap using `admin`, plain API over the Internet, outdated RouterOS (including CVE-2018-14847).
- Revenue and abuse: vouchers disabled in TapTap but still working on the router, vouchers that failed to publish, codes logged in on more devices than the plan allows, bypassed IP bindings, MikroTik users that never expire, unmanaged routers on customer ports.
- Every finding shows the RouterOS command to fix it; safe items (Telnet, FTP, SOCKS, UPnP, bandwidth server, open proxy) have a one-click fix that is logged in the change audit.

### Deploy anywhere
Everything is set from `.env` (see `.env.example`):

| Setting | Purpose |
|---|---|
| `ALLOWED_HOSTS` | Every hostname/IP people use. CSRF trusted origins are derived automatically (http + https). |
| `CSRF_TRUSTED_ORIGINS` | Only needed for non-standard ports, e.g. `http://192.168.88.10:8000`. |
| `SECURE_COOKIES` | `1` behind HTTPS, `0` for plain-http LAN installs (otherwise login fails). |
| `WEB_BIND` | `8000` for all interfaces, `127.0.0.1:8001` behind a host nginx/Caddy. |
| `DB_ENGINE=sqlite` | Single small box without PostgreSQL (WAL mode, safe for parallel discovery). |
| `CELERY_EAGER=1` | No Redis/worker available: syncs run inside the request. |
| `MIKROTIK_TIMEOUT` / `MIKROTIK_LIVE_TIMEOUT` | Upper bound for discovery / live polls per router. |
| `MIKROTIK_SSL_VERIFY` | `0` (default) accepts RouterOS self-signed api-ssl certificates. |
| `GUNICORN_WORKERS` / `GUNICORN_THREADS` | gthread workers keep the site responsive while routers answer. |

TapTap connects **out** to each router's API port. A router with a private address (10.x, 192.168.x, 172.16–31.x, 100.64.x) is only reachable when TapTap runs on the same network or over a VPN (WireGuard, SSTP, OpenVPN). The connection also accepts `host:port` in the address field and falls back to the legacy login for RouterOS older than 6.43.

### Why the Django build polls instead of streaming
The Node.js build kept long-lived connections open to push updates. Django behind gunicorn serves short request/response cycles, so this build uses short, bounded requests (`/topology/graph/`, `/topology/refresh/<id>/`, `/topology/live/`, `/routers/<id>/telemetry/`) plus a shared cache. That keeps every worker free, survives proxies and load balancers, and behaves the same on any host.

### Upgrade
```bash
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py showmigrations core   # 0005_security_ack and 0006_voucher_plan_name_length [X]
```

## Finance, Reports and the Studios

Run `python manage.py migrate` after updating — migration `0007_finance_and_studios` adds the new tables and fields.

### Finance (`/finance/`)
- **Sales ledger.** Record sales from stock (oldest unsold vouchers of a plan), by voucher code, or as a plain amount. Payment methods include cash, Wave, QMoney, Afrimoney, bank and card. Voiding a sale puts the voucher back in stock.
- **Automatic sales.** When router sync first sees uptime on an unsold voucher, it marks it used and (if *Record sales automatically* is on) books a sale at the plan price. A voucher is never booked twice.
- **Expenses** by category and site, with monthly bills copied forward in one click.
- **Agents / resellers** with commission %, cash hand-ins and a running balance still to be handed in.
- Overview charts, monthly target with pace projection, 12-month P&L, CSV exports (sales, expenses, P&L, agents).

### Reports (`/reports/`)
Date presets or custom range, filter by router and plan. Revenue/volume trend, voucher journey (generated → sold → activated), plan mix, weekday × hour heatmap, top sites and sellers, unsold stock by age, automatic highlights, CSV and print.

### Portal Studio (`/studio/portal/`)
Design the hotspot **login**, post-login **redirect** and **status** pages from 19 templates and 17 block types, with live phone/tablet/desktop preview, undo, autosave and publishing.

Two ways to use a page on a MikroTik:
1. **Offline** — *Export → Router files* gives `login.html` (+ `alogin.html`, `status.html` from your default pages). Upload to the router's hotspot folder. Supports HTTP-CHAP. Prices are baked in: re-export after changing plans.
2. **Hosted** — *Export → Hosted redirect* gives a small `login.html` that forwards customers to `https://<your-taptap>/p/<slug>/`, plus `taptap-walled-garden.rsc`. The hosted page checks the code first (rate-limited to 20 tries/min per IP) and logs in with HTTP-PAP, so enable `http-pap` on the hotspot profile.

Custom fonts load from Google Fonts before login only if the walled-garden entries are added; otherwise the page falls back to system fonts.

### Voucher Designer (`/studio/vouchers/`)
Drag-and-drop card designer in millimetres: text with tokens (`{code}`, `{plan}`, `{price}`, `{ssid}`, `{serial}`…), voucher code, QR code (scan to log in / show code / join Wi-Fi), logo, shapes, lines and icons. 12 templates including 58 mm thermal receipts and a 40-per-A4 pocket slip. Print sheets (`/studio/vouchers/print/`) lay cards out on A4, Letter, A5 or receipt rolls with cutting lines; open them from Batches, Vouchers (selected or unsold), after generating, or after recording a sale.

Set the Wi-Fi name, hotspot login address, help phone, logo and brand colour in **Settings** — both studios use them.

Chart.js 4.4.4 and qrcode-generator 1.4.4 are vendored under `static/vendor/` so everything works on offline LAN installs.
