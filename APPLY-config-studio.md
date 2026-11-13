# TapTap update — MikroTik Config Studio

**Migration: 0121** adds two tables (`RouterStudioBoard`, `RouterStudioDeploy`). No new packages.

Built on `master` at `91da232`. New page: **Routers → ⋯ → Config Studio** (also the "Studio" chip on each router card and a
button in the Control Center) — `/routers/<id>/studio/`. Needs the *network.manage* permission.

## What you get

* **Tiles that read like sentences.** "Guests on `ether5` get `10.10.10.1/24`, kept away from my network: `yes`, sharing
  `10M` down." 22 tiles in six families: Internet, Networks, Hotspot, Speed, Protection, Router. Drag a tile onto the board,
  or onto a port of the router drawing to fill that port in. Starter kits fill a whole board.
* **Say it instead.** The box above the board turns plain words into tiles ("guest Wi-Fi on ether5 at 10 Mbps and block
  tiktok.com"). It uses Fit's AI keys (`GEMINI_API_KEY` / `GROQ_API_KEY`); without a key a built-in phrase reader handles the
  common requests. The helper can only add, change or remove tiles.
* **The RouterOS code, live.** The right-hand panel shows the commands the tiles stand for; hover a tile to light up its
  lines. "Copy" hides passwords; ".rsc" downloads a script that is safe to run twice.
* **A virtual router.** Every change is tried on a copy of the router's configuration: the drawing repaints, and packets are
  sent through hotspot, port forwarding, routing, firewall, NAT and queues ("a guest tries your printer → stopped by rule …").
  It also checks that TapTap itself can still reach the router. "Send your own packet" tries anything you like.
  Two practice routers (factory default, empty) let you build without a real one.
* **Every setting.** All menus TapTap read from the router, searchable, as they are *now* or *after my board*, with new,
  changed and removed rows marked. The pencil turns a row into an "Expert row" tile.
* **Back up → deploy → keep or undo.** Deploy needs a backup from the last 60 minutes. An undo timer is put on the router
  first (`/system scheduler TapTap-Studio-undo`): if nobody presses **Keep** in time, the router puts the old values back
  itself. A refused row takes back the rows before it. Works over the RouterOS API, TapTap Tunnel and TapTap Link (one
  piece per check-in). Every deploy is listed with its own Undo.

## What it does not do

* Wi-Fi radio settings (wireless / wifi / CAPsMAN differ too much between models) — use the Expert row tile or WinBox.
* The Expert row tile is limited to the menus the Control Center's Advanced Explorer may already write
  (`MikroTikService.WRITE_PREFIXES`). Scripts, schedulers, users and files cannot be touched from a tile.
* The virtual firewall judges the everyday matchers. A rule using anything else (layer-7, time, limits…) is shown as
  "cannot be judged here" and skipped, never guessed.
* Passwords are not put into the automatic undo: undoing a PPPoE tile restores the user name but not the old password.

## Not yet tried on a real MikroTik

The deploy engine is tested against a stand-in router (`core/tests/test_config_studio.py`), not real hardware. Before
relying on it, deploy one harmless tile (Router name) to a test router over each transport you use, press Undo once, and
let one undo timer run out on purpose.

## Server

    cd /opt/taptap
    unzip -o ~/taptap-config-studio.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core
    docker compose up -d --force-recreate web worker vouchers live

## Files

New: `core/config_studio.py` (views, deploy engine, undo timer, Link command), `core/config_studio_tiles.py` (tiles → rows →
RouterOS text), `core/config_studio_sim.py` (virtual router), `core/config_studio_ai.py` (helper), `core/models_config_studio.py`,
`core/migrations/0121_config_studio.py`, `templates/core/config_studio.html`, `static/js/config-studio.js`,
`static/css/config-studio.css`, `core/tests/test_config_studio.py`.

Changed: `core/apps.py` (two lines: load the models, install the add-on), `templates/core/routers.html` (menu entry and
chip), `templates/core/router_control.html` (button).

## Tests

    python manage.py test core.tests.test_config_studio      # 35
