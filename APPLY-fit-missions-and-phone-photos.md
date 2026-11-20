# TapTap update: Fit missions, Fit email, Fit tables & workspace, photos kept on the phone, dates in chat

**Migrations 0127 and 0128** (two new tables). No new Python or app packages. Built on master **d885bd5**. The zip holds the
**complete files** that change, in their folders.

## 1. Fit missions — jobs Fit does later, by itself

You tell Fit what to do **and when**; Fit shows one card; you press *Take action*; from then on it waits for its
moment, does the job, and tells the people you chose. Examples Fit understands:

* *"Tonight between 01:00 and 05:00, restart Mast when fewer than 3 people are online, and email Awa when it is back."*
* *"Every Monday at 08:00, look at last week's agent sales and email me what you think."*
* *"When Tower has been offline for 10 minutes, tell the team in chat and email Lamin."*
* *"When sales today reach D5,000, tell me."* · *"When Daily stock falls under 30, email me."*
* *"At midnight, disable every frozen voucher."* · *"In 45 minutes remind me to collect the cash from Tower."*

### When

| Trigger | What it means |
|---|---|
| **at** | one moment: "23:00" (the next time it comes), "2026-11-24 03:00", or "in 45 minutes" |
| **repeat** | a timetable: every day · chosen weekdays · a day of the month, at a time |
| **quiet** | the quietest moment of a window, on one router or all. With *max_online*: as soon as at most that many people are online; if it never gets that quiet, at the end of the window it **runs anyway** or **skips** (your choice). Without: same rule as router schedules — at once when nobody is online, after two thirds of the window at the quietest moment seen, at the end whatever happens. Once or every day. |
| **when** | something that has been true for some minutes: people online at/below or at/above N · a router offline · a router back online · router CPU high (TapTap Link) · sales today reach an amount · stock of a plan below N · a voucher/member comes online · customer reports waiting. Optionally only inside a window of the day. Once, or **every time** it happens again (only after it was false again, and not more often than its cool-down). |

### Do

| Job | |
|---|---|
| **notify** | only tell people (your message) |
| **restart_router** | the same restart as the Reboot button and router schedules. TapTap then follows it and tells the mission's people **when the router is back** (or that it is not back). |
| **shutdown_router** | a real power-off; Fit must warn you and you must say you understand |
| **action** | any change Fit can offer: freeze, add time, sync, router change, bulk voucher changes… Checked when you approve, and again **with the situation at that moment** when it runs. A group change that would then touch far more vouchers than when you approved (more than 2× and +50) **stops and asks you**. Each run is also written as a Fit action (title ends "(mission)"). |
| **report** | overview · sales (today vs yesterday at the same time, best plans) · network (each router, people online, CPU) |
| **ask_fit** | a question Fit answers at that moment with its **read-only** tools; the answer is sent on. Fit cannot propose or change anything during that run. |

### Tell

Bell + phone push to you (default) or the whole team · a note in the **team chat room** · **email** to "me", team
members by name, or — Owner/Admin only — any address (at most 6) · a **heads-up** N minutes before a timed run.
The email has its own small template (`core/email/fit_mission.html`), not the business notification email.

### Safety

* A mission exists only after a person approved its card (or set it on the Missions page).
* **Every run checks again** that the person who set it is still in the business and still has the right, that Fit is
  on, that the owner allows Fit **Missions**, and the group of the change (e.g. *Router control* for a restart).
  Otherwise it fails and says exactly why.
* Times passed while the server was down: reported as **missed** (30 minutes grace), never run hours late.
* At most 40 waiting missions per business.

### Where

* **Fit settings:** a new group **Missions** (the owner switches it on).
* **Web:** `/fit/missions/` — every mission with When / Then / Tells, next time, what TapTap sees now, runs, last
  result and log; **Now / Pause / Resume / Cancel**; and a form to set one without Fit. Linked from the Monitors page
  and from every mission card.
* **Phone app:** More → **Fit missions** (same list and buttons). Mission cards from Fit show on the phone as before.
* Fit has a new read tool `missions` and four new actions: `create_mission`, `pause_mission`, `resume_mission`,
  `cancel_mission`. Who may change a mission: the person who set it, the Owner or an Admin.

A new Celery beat job runs every minute: `taptap-fit-missions` (`core.tasks.fit_missions_task`).

## 1b. "Send it to my email" — Fit emails now

Fit used to answer *"I don't have an email tool"*. Now it offers a **send_email** card: it writes the subject and the
text itself (the summary it just gave, the numbers, the next steps), and the card shows **To**, **Subject** and the
**Text** before anything is sent. Press *Take action* and it goes out.

* To: **me** (the default), team members by name, or — Owner/Admin only — any address.
* Fit's markdown is made plain for the email (bold removed, `[Open routers](/routers/)` becomes
  *Open routers (https://taptapnetwork.com/routers/)*, using `SITE_URL`).
* New group in Fit settings: **Email**. The owner switches it on, like every other group.
* Later or every day / week: Fit uses a mission instead (*"email me this summary every evening at 21:00"*).

## 1c. Fit tables, custom filters and the Fit workspace

Ask Fit for a table — *"make a table of vouchers older than a month"*, *"frozen members on Tower"*, *"this month's sales
of Lamin, biggest first"*, *"routers that are offline"* — and it builds a **saved, filtered table**:

* **In the chat** it shows as a card: title, how many rows, the filters (*Older than 30 days · State: unused*), summary
  chips and the first rows. **Click the card** to open the table.
* **Its own page** `/fit/tables/<id>/`:
  * sort by clicking any column header
  * **Filters** panel: change any filter live (state, plan, batch, router, agent, members, age, end date, search…)
  * **Columns** picker (18 voucher columns: price, batch, agent, age in days, devices, customer, serial…)
  * **summary chips** that narrow the table when clicked (*Frozen 12*, *Daily 40*, *Total D12,400*)
  * click a row to open the voucher (or the router)
  * **CSV** download (opens in Excel)
  * **Save these filters**, **Save as new**, **📌 Pin to workspace**, share with the team / make private, delete
  * **Ask Fit about it** — Fit reads the table and tells you what stands out
* **Always live:** a table keeps the question, never a copy. Each time it opens it runs again, and relative filters
  stay relative (*older than 30 days* is always counted from today).
* **Sources:** vouchers & members, sales, routers, customer reports.
* **Privacy:** a table opens with the rights of the **viewer**. Someone who cannot see sales never sees a sales table,
  even one the owner shared. A private table is only for its maker. Only the maker, the Owner or an Admin can change
  one; anyone who can see it can *Save as new*.

**Fit workspace — one click to everything Fit built:**

* A new **workspace button** (⊞) in Fit's header opens a view inside the Fit panel: Tables · Dashboards · Missions ·
  Monitors as four buttons, your tables and dashboards as clickable rows, waiting missions, and one-tap ideas
  (*Vouchers older than a month*, *Frozen & warned*, *Members ending this week*, *Sales this month*, *Offline routers*,
  *A live dashboard*, *Something else…*).
* A full page `/fit/workspace/` with the same, linked from every table.
* Any page can open Fit with a question (`data-fit-ask="…"`), as the idea buttons do.
* Fit's new tools: `create_table` (also to change one, with `table_id`), `tables`, `table_data`. Fit suggests the next
  step in one line: a dashboard, a weekly email mission, or a group change on exactly those vouchers.

**Phone app:** a ⊞ button on the Fit screen opens the workspace. Tables open **in the app**: sortable columns, chips to
narrow, search, pages, and a tap on a voucher opens the voucher. Dashboards open in the browser. A table card in
Fit's chat opens the table, and a dashboard card opens the dashboard. Ideas picked in the workspace are asked to Fit.

## 2. Chat photos kept on the phone until their normal time (app 1.8.0)

* Every photo sent in chat is **downloaded into the app's own private storage** as soon as it arrives while the app
  is open (in any screen, not only its conversation) or when its conversation is opened. It then shows at once and
  **opens without internet**. Tap it for full screen with pinch-to-zoom.
* **Deleted at the normal time:** TapTap deletes chat photos after 7 days (`CHAT_IMAGE_DAYS`); the phone deletes its
  copy **at that same moment, by itself** (it uses the photo's own expiry from TapTap, checked on opening a chat and
  every 15 minutes). Under each photo: *"📱 On this phone · deleted Fri 27 Nov 14:00"*.
* **Listed:** More → **Photos on this phone**, grouped by day, with "6 d left" on each. Long-press one to delete it
  earlier; **Delete all** at the top. Long-press a photo in chat → **Delete from this phone**.
* **View-once photos are never kept.** They now show *"View-once photo — tap to open"* and open once, full screen,
  from memory. (Before, the app opened them the moment the bubble was drawn.)
* Signing out deletes them all, like everything else the app keeps for an account.

## 3. Dates in chat (app)

* A date line between messages of different days: **Today**, **Yesterday**, the weekday for the last week
  (**Wednesday**), then **Fri 13 Nov**, and the year when it is not this year (**Mon 2 Dec 2025**).
* Long-press a message: the top line says who sent it and the full date — *"Sent by Lamin · Fri 20 Nov 2026 at 14:03"*.
* The web chat already had date lines; it is unchanged.

Also fixed on the phone: Fit cards with facts (group changes, missions) showed them as `[Matched, 12]`; they now read
**Matched** 12.

## Server

    cd /opt/taptap
    unzip -o ~/taptap-fit-missions-photos.zip -d /tmp && cp -r /tmp/taptap/. /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core      # 0127 missions, 0128 tables
    docker compose up -d --force-recreate web worker vouchers live

Celery beat picks up the new job when its container is recreated above. Reload TapTap with **Ctrl+F5** (new
`fit.js?v=tables1`, `fit.css?v=tables1`). Then, as the owner: open Fit → ⚙ settings →
switch on **Email** and **Missions** (and **Router control** if missions should restart routers). Emails need the
server's mail settings (`EMAIL_HOST`…) and `SITE_URL`, as for the notification emails.

## Phone app (1.8.0)

    cd mobile && flutter pub get && flutter run      # or push: the GitHub workflow builds the APK

No new package. The Dart code was checked by eye only: the Dart SDK could not be downloaded in this workspace. The
GitHub build (`flutter analyze`, `flutter test`, APK) is the real test.

## Tests

    python manage.py test core.tests.test_fit_missions core.tests.test_fit_tables

`test_fit_missions` (17 tests): building from what the person said (relative times made real, same mission on
re-check, past times / unknown conditions / un-understood shutdown refused); outside emails only for Owner/Admin;
restart needs the network right; heads-up once, then email + team chat; missed after downtime; timetable goes on;
quiet restart at ≤ N online with the router-back email; skip when never that quiet; quietest moment without a number,
every day; condition every time (not twice for one outage, again after it was fine); stock condition; rights and owner
switches checked at run time; a Fit change run by a mission (and refused when its group is off); propose + approve
through Fit, the `missions` tool, read-only Fit cannot propose; the minute tick; the page and the app API (create,
pause, resume, someone else refused, run now); send_email: nothing sent before approval, refused until the owner allows
Email, then sent to me + Lamin with the markdown made plain, outside address refused for a team member.

`test_fit_tables` (4 tests): Fit builds "old unused vouchers" (relative filter kept, oldest first, card with link and
filter words, a bad plan explained); the page with live filters, sort, chips, columns, CSV, save, pin, the phone's JSON
and the workspace; a sales table with a minimum amount, refused to a role without sales rights, a private table
refused to others; an offline-routers table.

Full suite: 879 tests. The only failures are the same 38 that fail on master (they need a Redis cache this workspace
does not have).

## Files

New: `core/fit_missions.py`, `core/fit_tables.py`, `core/migrations/0127_fit_missions.py`, `core/migrations/0128_fit_tables.py`,
`core/tests/test_fit_missions.py`, `core/tests/test_fit_tables.py`, `templates/core/fit_workspace.html`, `templates/core/fit_table.html`,
`mobile/lib/screens/fit_workspace_screen.dart`,
`templates/core/fit_missions.html`, `templates/core/email/fit_mission.{html,txt}`, `mobile/lib/data/photo_store.dart`,
`mobile/lib/screens/{chat_photo,saved_photos_screen,missions_screen}.dart`
Changed: `config/settings.py`, `core/{features,fit_actions,fit_ai,fit_tools,models,models_fit,permissions,tasks,urls,views_fit}.py`,
`templates/core/{base,fit_monitors}.html`, `static/js/fit.js`, `static/css/fit.css`, `mobile/lib/app/{api,format,state}.dart`,
`mobile/lib/screens/{chat_screens,more_screen,fit_screen}.dart`, `mobile/pubspec.yaml`
