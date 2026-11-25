# New landing page, with adverts you run from the platform console

**Migration 0132** (one new table: `core_landingad`). No new packages (Pillow is already installed).

## The landing page (`/`)
It is a page of its own, no longer the app layout, and it loads nothing from outside servers.

- **Announcement bar at the top.** Visitors can close it, and they don't see that announcement again.
- **Hero:**
  - "Your Wi-Fi business, on autopilot."
  - A living network: a MikroTik in the middle, with phones, laptops and a TV around it and data packets running
    both ways.
  - A voucher code that types itself every few seconds, a "+D50 voucher sold" chip and a "customers online"
    counter.
- **Live figures** (businesses, routers online now, vouchers sold in 30 days, countries). They are real numbers,
  rounded down and cached for 10 minutes. They appear only once TapTap has `LANDING_STATS_MIN` businesses
  (default 10).
- **Six feature cards** that light up under the pointer.
- **"Three steps. No technician."** with an animated path.
- **The four plans** (Free, Basic, Pro, Enterprise) with "from" prices from the Gambia price list.
- **FAQ**, a final sign-up band and a footer.
- **Signed-in users** see "Open TapTap", which goes to their dashboard, or to the console for platform staff.
- **Phones:** the page works on phones, and animations are off for people who turn motion off.

## Adverts, from Platform console › Landing & adverts
**Four places on the page.** Click one on the page map to start an advert for it:

| Place | Shows |
|---|---|
| Announcement bar | one at a time, top of the page |
| Hero card | one at a time, next to the headline (under the buttons on phones) |
| Wide banner | one at a time, between sections |
| Offers & news carousel | up to 6, slides by itself, with arrows |

**Each advert has:**
- **Text:** a title, a short text, a button text and a link.
- **Who and how it is labelled:** the advertiser, and a label (NEW, -20 %…). Without a label it says "Sponsored"
  when it has an advertiser and "News" when it is TapTap's own.
- **Look:** 5 colour styles, and an optional picture. Upload any JPG, PNG or WebP; it is made small (WebP, at most
  1600×900) and cached by browsers.
- **When and how often:** a start and end time, a weight (1–10, how often it shows when several share a place), and
  on/off.

**The editor:**
- A live preview card updates as you type.
- **Preview on the landing page:** the eye button opens `/?preview=<id>`. Only platform staff see the ad there,
  even when it is paused or scheduled.
- **For each ad:** views, clicks and click rate, plus Pause / Switch on, Copy (the copy starts paused), Reset
  counts and Delete.

**Counting views and clicks:**
- **Views** are counted for real visitors only, not for bots or link previews (WhatsApp, Facebook) and not for
  platform staff.
- **Clicks** go through `/go/ad/<id>/`. TapTap counts the click, then forwards the visitor, to http/https links
  only.

## Also fixed
`FREE_PLAN_FALLBACK` and `PLAN_BASIC_RATIO` from the plans update are now really read from the environment (they
are now in settings.py), along with the new `LANDING_STATS_MIN`.

## Removed (no longer used)
- `templates/core/home.html`
- The old landing styles in `static/css/app.css` (`.landing`, `.hero-dashboard`, `.mini-*`, `.feature-strip`…).
  Styles other pages still use were kept.

## Files
- **New:**
  - core/landing.py
  - core/models_landing.py
  - core/migrations/0132_landing_ads.py
  - core/tests/test_landing.py (11 tests)
  - templates/core/landing.html
  - templates/core/_landing_ad.html
  - templates/core/platform/landing.html
- **Changed:**
  - core/views.py (`home`)
  - core/apps.py
  - core/models.py
  - config/settings.py
  - static/css/app.css
  - templates/core/platform/base.html (menu link)
- **Deleted:** templates/core/home.html

## Apply
    git apply taptap-landing.patch          # or copy the files from the zip, and delete templates/core/home.html
    python manage.py migrate
    # restart web

## Tests
The full suite passes: **1021 tests, 0 failures** (`CACHE_URL=locmem://`).
