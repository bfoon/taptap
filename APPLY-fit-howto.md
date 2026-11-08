# TapTap update — Fit's "How to": guided tours across the whole app

**No migration.** No new packages.

This builds on branch `voucher-unstick`. The zip has everything since master: the voucher fix, vouchers first and
Fit's wardrobe (migration 0118, if you have not applied it yet).

## What it does

Open Fit and press the **compass** button (or **🧭 How to…** on Fit's welcome). Then:

1. **Pick a job.** Search, or browse by group:
   * Routers & TapTap Link
   * Vouchers & plans
   * Members
   * Network & security
   * Login page & adverts
   * Money, team & business
   * More guides
2. **Fit points the way.** It throws something at the next thing to press, or flies there:
   * 🔱 a spear
   * 🏹 an arrow
   * 🔦 a light-gun beam
   * 💦 a water-gun jet that splashes
   * ⚡ lightning from its antenna (the screen flashes)
   * 📄 a wobbly paper plane
   * 🛩️ a little single-seat plane with one front propeller — Fit flies it there itself, leaving a smoke trail, and
     flies back home

   **Mix it up** changes the throw every step. The chips under the search box set your favourite.
3. **The thing to press is shown.** It gets a blue ripple, a pulsing outline, and a hand that keeps tapping it. A tip
   card next to it says what it does and why. It has:
   * **Step 2 of 6** and progress dots
   * **Back**, **Next** and **↺** (show me again)
   * **×** to end the tour (or press Esc)
4. **It follows you across pages.**
   * Press the real button (or **Take me there**) and the tour carries on on the next page.
   * It goes into a router's TapTap Link page, into a voucher, through forms and dialogs.
5. **It finds hidden things.**
   * **On a phone**, Fit first points at the **☰** button, then at the item inside the menu.
   * **Inside a dropdown** ("More", the router card's ⋯), it points at the dropdown first.
   * **Inside a dialog or a tab**, it points at what opens it, then follows when it opens.
   * **If something is not on the page** (switched off, or not for your role), it says so instead of pointing at
     nothing.
6. **When you finish,** Fit hops and says *"Done! That's how you …"*.

## Tours included

These are the ones built step by step with exact targets:

* **Routers:**
  * Add a router to the business
  * Rotate a router's Link token
  * Troubleshoot a TapTap Link router
  * Sync a router
  * Schedule a restart or shutdown
  * Test the network from the router
* **Vouchers & plans:**
  * Create a plan
  * Generate and print vouchers
  * Make one voucher
  * Find a voucher and fix it (Can't connect?, Add time, More)
  * Batches
  * Restore from the bin
* **Members:**
  * Add a member
  * Renew and record the payment
  * Create a Member Plan
* **Network & security:**
  * Live sync
  * Fair usage
  * Active users
  * Devices
  * IP bindings
  * Topology
* **Portal & adverts:**
  * Publish your login page
  * Adverts
  * Voucher card designs
* **Business:**
  * Give staff a login
  * Agents and cash
  * Expenses and profit
  * Daily sales
  * Reports and exports
  * Logo, colours and Wi-Fi name
  * Features
  * Chat
  * Bonanza

**Every How-To guide on the Support page is also a tour**, so everything with a guide can be shown.

**Each person sees only the tours for pages their role can open.** For example, voucher support staff do not get
the router tours, and View-only staff get no router or team tours.

### Ask in the chat

When you ask Fit *"how do I rotate the token?"* or *"how do I add a member?"*, it answers and puts a
**▶ Show me** button under its reply. That button starts the same tour.

## Server

    cd /opt/taptap
    unzip -o ~/taptap-fit-howto.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core
    docker compose up -d --force-recreate web worker vouchers live

## Adding a tour later

Add an entry to `TOURS` in `core/fit_tours.py`. Each step names a page (its URL name) and what to point at:

* `text:Button text` — a button, link or tab by its text
* `field:name` — a form field
* `sel:#css` — anything, by CSS selector
* `nav` — the menu item

Pages that have an id in their address (a router's Link page, a voucher) use `match` and `go`.

## Tests

    python manage.py test core.tests.test_fit_tours

The tests check that every page in every tour exists and that each person gets only their own tours. They also check
that every Support guide is a tour and that Fit offers "Show me".

All the main tours were also played in a real browser against the real pages, on desktop and on a phone screen:

* add a router
* rotate token
* troubleshoot Link
* find a voucher
* add staff
* generate vouchers
* add a member
* publish the portal
* network tools
* agents
