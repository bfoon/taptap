# Plans (Free · Basic · Pro · Enterprise), and no more blank screen after signing in

**Migration 0131**: it adds `Subscription.tier` and the `EnterpriseFeature` table, and renames old package
names. Restart web, worker and beat afterwards. No new Python packages.

## 1. The blank screen after signing in
Every page waited for Bootstrap and its icons from an outside CDN (cdn.jsdelivr.net) before it could draw. On a
slow or blocked connection to that CDN, the browser showed a white page until the files arrived or timed out.
- **Now served by TapTap itself:** Bootstrap 5.3.8 and Bootstrap Icons 1.13.1 come from `static/vendor/`. These
  are the same versions as before, with their licences included.
- **Every page switched over:** the app, platform, chat, and voucher print pages no longer load anything from
  outside servers. With every outside request blocked, the dashboard still draws completely.
- **Smaller downloads:** `collectstatic` now also writes compressed copies (.gz, and .br when brotli is
  installed). WhiteNoise sends those, so the files are a fraction of their size (`STORAGES` in settings.py).

## 2. Plans
| Plan | What it unlocks | Price |
|---|---|---|
| **Free** | Vouchers, batches, plans, generate, print (minimal design), bin, routers over TapTap Link, active users. Everything else is locked. | Free forever |
| **Basic** | Everything except team accounts, payroll and Fit (the AI assistant). | `PLAN_BASIC_RATIO` (default 0.6) × the Pro price, rounded |
| **Pro** | Everything. | Your current price list (unchanged) |
| **Enterprise** | Everything, plus features TapTap builds for that business. | Agreed with TapTap |

### Locked features
- **Menu:** locked links stay in the menu, dimmed and with a lock, so owners see what a plan adds. Modules the
  owner hides still disappear.
- **Locked pages:** a locked page says "Reports comes with Basic", lists what that plan includes, and has an
  **Unlock with Basic** button.
- **Top pill:** shows "Pro · 30 days left" or "Free plan · upgrade".
- **Printing on Free:** always uses *Paper Minimal*, whatever designs were saved before.

### Subscription page (now "Plans & subscription")
- Four plan cards; your plan is marked. Basic and Pro have one button per duration, with the price and the saving.
- A table showing which module each plan includes.
- **Enterprise:** owners describe what they need. They follow each request through requested → planned → being
  built → live, with TapTap's answer. On the platform console (Business → *Tailor-made features*) you move each
  request along and write the answer.

### How a business moves between plans
- **New sign-ups:** they start on Pro for the trial.
- **When the trial or paid time ends:** the business falls back to **Free** instead of being locked out. Vouchers
  keep working; the rest locks.
  - Set `FREE_PLAN_FALLBACK=0` to keep the old lockout.
- **Buying a plan:** choosing Basic or Pro creates a payment request. The plan starts when you confirm the payment.
- **Payments recorded on the platform console:** you can pick a plan. Without a plan, the business keeps its plan,
  or goes from Free to Pro.
- **Old package names:** Starter becomes Basic, and Business becomes Pro. Nobody loses a module.

### Work that stops on Free
When a plan doesn't include a module, that module's background work stops, as it did before for lapsed accounts:
- Fit missions and monitors
- Internet-sharing checks
- App & site rules
- Business alerts
- the agent voucher checker

Vouchers, expiry and live checks keep running. Saved **Sync control** choices are paused on Free and TapTap's
defaults apply; vouchers waiting to be sent go out.

### New modules (so Free can lock them)
- **Router control & Config Studio**
- **Schedules & sync control**
- **Flow lab**
- The line hub now belongs to *Internet lines*.

Basic and Pro include all of these.

## Files
- **New:**
  - core/plans.py
  - core/models_plans.py
  - core/migrations/0131_plans_free_basic_pro_enterprise.py
  - core/tests/test_plans.py (20 tests)
  - static/vendor/bootstrap/
  - static/vendor/bootstrap-icons/
- **Changed:**
  - core/features.py
  - core/models.py
  - core/subscriptions.py
  - core/views.py
  - core/urls.py
  - core/permissions.py
  - core/middleware.py
  - core/views_platform.py
  - core/views_features.py
  - core/views_studio.py
  - core/sync_control.py
  - core/live.py
  - core/fit_missions.py
  - core/fit_monitors.py
  - core/views_agent_portal.py
  - config/settings.py
  - static/css/business.css
  - templates: base, subscription, feature_off, business_features, platform/base, platform/business_detail,
    platform/subscriptions, chat_standalone, studio/voucher_print

## Apply
    git apply taptap-plans.patch        # or copy the files from the zip
    python manage.py migrate
    python manage.py collectstatic --noinput   # the entrypoint already does this
    # restart web, worker, beat

## Tests
The full suite passes: **1010 tests, 0 failures** (`CACHE_URL=locmem://`).
