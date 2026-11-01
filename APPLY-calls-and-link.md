# TapTap update — calls reconnect, TapTap Link short install, big voucher batches

Built on commit b9f1aa0 (1 Nov 2026). No database migration.

    cd /opt/taptap
    unzip -o ~/taptap-calls-and-link.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose up -d --force-recreate web worker vouchers live

Optional TURN relay for calls (recommended) — add to .env:

    CHAT_TURN_SECRET=<long random string>
    CHAT_TURN_URLS=turn:turn.taptapnetwork.com:3478?transport=udp,turn:turn.taptapnetwork.com:3478?transport=tcp
    TURN_EXTERNAL_IP=<server public IP>

then: docker compose --profile turn up -d turn && docker compose up -d --force-recreate web
(turn.taptapnetwork.com must be DNS-only, not Cloudflare-proxied; open 3478/udp+tcp and 49160-49359/udp)

## 1) Calls and screen sharing — no more drops on a short network blip
* A dropped line reconnects with an ICE restart (same call, new path) instead of hanging up; gives up after 60 s.
* Also reconnects when the tab comes back from the background or the network comes back online.
* Server ends a call as "lost" after 90 s of silence (was 40 s).
* Screen share drops to 450 kbps / 5 fps on a weak line (was fixed at 1.5 Mbps / 15 fps).
* Optional coturn service in docker-compose (profile "turn").

Changed: core/chat_call.py, core/views_chat.py, static/js/chat-call.js, docker-compose.yml

## 2) TapTap Link — short "just connect" install
* Link page (Set up / Rotate token) now has two tabs: Short — just connect, and Full install (unchanged).
* Short: clock + HTTPS check (built-in trust store) + the TapTap Link heartbeat only. Same token as the full one.

Changed: core/agent.py, core/views_link.py, templates/core/router_link.html

## 3) Voucher batches above 500
* Generate Vouchers takes any amount. Above 500 it shows how many vouchers, the plan, price each and the total
  value, and creates them only after you confirm that exact amount (a browser prompt; a confirm page as fallback).
* Settings (optional, .env): VOUCHER_CONFIRM_ABOVE (default 500) — when to ask;
  VOUCHER_BATCH_MAX (default 50000) — technical ceiling for one batch, so a mistyped number cannot stall the server.
* Agent orders and "sell from stock" keep their 500 limit.

Changed: core/views.py, templates/core/generate_vouchers.html. New: core/tests/test_voucher_big_batch.py
Tests: python manage.py test core.tests.test_voucher_big_batch

## Link router dropping off?
If "Only accept public IPs" lists one IP and the router has two internet lines, list both (comma-separated)
or clear the field — check-ins from the other line are refused and look like "Not checking in".
