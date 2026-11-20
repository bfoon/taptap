# TapTap update: "the internet goes off for a few seconds and comes back"

**Migration 0129** (a new test kind: choices only, no data change). No new packages. Built on master **78e47f7**.
The zip holds the **complete files** that change, in their folders.

## 1. The bug: Internet Doctor v2 cut lines that were working

The Internet Doctor runs on the router every minute. In v2 it decided a line was **"down"** as soon as the
**modem did not answer ping**. Many modems never answer ping: some fibre and LTE boxes, ISP CPEs, and Starlink in
some modes. The line still carried every customer, but the Doctor treated it as broken:

| v2 did | What people saw |
|---|---|
| **Renewed its DHCP every 15 minutes** (`fail % 15 = 5`), even though the line was bound and working | The internet off for a few seconds, then back. Again 15 minutes later, all day |
| With two lines: after 3 minutes, **took the working line out of use** and moved everyone to the other line | A short drop when everyone moved, and the other line overloaded. Then the same again when it was put back |

A provider that blocks ping to the internet set off the same chain one step later. The Doctor called the line
**"holding"** and escalated: ARP clear, DHCP renew, a new MAC address. Each step drops the line for a few seconds.

### Doctor v3

* **A modem that ignores ping is not a broken line.** The line is judged only by its 3 internet test addresses,
  pinged through that line.
* **Ping blocked?** The test addresses are DNS resolvers, so v3 also sends a DNS lookup straight to them through
  the line. If a lookup answers, the line is working.
* **DHCP is renewed only when the line really has no address** (status not *bound*), or when the internet really
  does not get through.
* The live check (Internet lines page, Fit) says *"the modem does not answer ping (that is normal for some modems),
  but 3 of 3 internet addresses answer"* instead of *"down"*.
* **Routers already running the Doctor are upgraded automatically.**
  * TapTap Link routers: within the hour, through the existing upgrade check.
  * Direct API / Tunnel routers: the next time TapTap reaches them.
  * Nothing to press.

## 2. New: "Why the internet blinks" — catches every short drop and says why

Network tools → **Why the internet blinks**. Fit runs it too (*"the internet keeps going off for a few seconds"*).

### The watch, on the router

It is installed the first time you run the test, and with the Internet Doctor.

* Every **5 seconds** it pings three public addresses. None of them is a line's health check or a Doctor probe, so
  it measures what customers get through whichever line is in use. If ping fails it tries one DNS lookup, because
  some providers block ping.
* When the internet comes back after a drop it writes a line to the router's log:

  > TapTap Watch: internet was out ~15s from 18:02:11 (cpu 12% routes 192.168.1.1%ether1 -> routes 192.168.8.1%ether2)

  It also keeps a short history that survives a busy log. A router restart is noted too.
* It costs one ping every 5 seconds. It is `/system script` + `/system scheduler` **TapTap-Watch**, and you can
  delete both in WinBox at any time.

### The detective, in TapTap

It puts each blink next to what the router's log says happened at that moment, then gives the causes ranked, each
with what to do:

| Cause | How it is recognised |
|---|---|
| **The router restarted** (power cut / *"without proper shutdown"*) | restart lines in the log, uptime |
| **A modem's cable or power** | the internet port went *link down / up* |
| **A port on your own network (the AP chain)** | a LAN port went down / up. In a chain of APs, one restart or one loose cable cuts everyone after it |
| **The modem took back the address** | *lost IP address* |
| **The provider's PPPoE session dropped** | PPPoE disconnected |
| **A loop in the network** | the bridge saw its own packets come back |
| **The router switched internet line** | the active route changed during the blink |
| **Internet Doctor repaired a line** | Doctor renew / new MAC lines |
| **The router was overloaded** | CPU 90 %+ when it stopped |
| **Many customers signed out at once** | 5+ hotspot sign-outs in the same minute |
| **The provider itself** | nothing changed on the router: the modem stayed connected and the internet behind it stopped. Starlink and mobile lines do this. You can send the provider the listed times |

The page shows:

* tiles: how many blinks, the total seconds off, the longest, and the router's uptime
* every blink: when, for how long, why, and the line before and after
* ports that flapped
* the router's own log lines, newest first

It works over Direct API, Tunnel and TapTap Link.

## Server

    cd /opt/taptap
    unzip -o ~/taptap-internet-blinks.zip -d /tmp && cp -r /tmp/taptap/. /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core
    docker compose up -d --force-recreate web worker vouchers live

Reload TapTap with **Ctrl+F5** (`nettools.js?v=5`, `nettools.css?v=4`). Then open **Network tools → Why the internet
blinks** on each router once, to start the watch. Run it again after the next blink.

## Tests

    python manage.py test core.tests.test_blink_watch core.tests.test_wan_failover

`test_blink_watch` (9 tests):

* **Doctor v3:** a silent modem is judged by the internet test; there is a DNS fallback; DHCP is released only
  without an address; the watch is installed with the Doctor (rsc and Link); the live check shows "working" for a
  silent modem.
* **The detective:** the AP-chain port, the modem port, CPU and line-switch blinks each get the right cause; a blink
  with nothing local is put on the provider; power cut and mass sign-out are found; a new watch installs once; the
  watch's own start line counts as a restart only right after a reboot; the full Link round trip.

Full suite: 888 tests. The only failures are the same 38 that fail on master (they need a Redis cache this workspace
does not have).

## Files

New: `core/blink_watch.py`, `core/migrations/0129_nettest_blinks.py`, `core/tests/test_blink_watch.py`
Changed: `core/wan_doctor.py`, `core/nettools.py`, `core/models_nettools.py`, `core/fit_tools.py`, `core/fit_ai.py`,
`core/tests/test_wan_failover.py`, `templates/core/network_tools.html`, `static/js/nettools.js`, `static/css/nettools.css`
