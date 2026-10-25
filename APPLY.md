# TapTap update — letter case, feature modules, country pricing, daily sales drill-down

## Install on the server (/opt/taptap)
    cd /opt/taptap
    git status                      # make sure you have no uncommitted work you'd lose
    unzip -o ~/taptap-update.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/   # or: git apply taptap-features.patch
    docker compose build web worker live
    docker compose run --rm web python manage.py migrate
    docker compose up -d

## What the migration does (core/migrations/0104_country_pricing_features.py)
- Business: country (existing = GM), feature_tier (existing = business = everything on), feature_overrides, features_off
- Subscription: currency (existing = GMD), country
- New table CountryPrice
Nothing changes for existing businesses until you change their package or switches.

## Note
Your repo already had model changes without a migration (memberpaymentintent / memberportalevent).
After this update `makemigrations` will create those as 0105 — that is unrelated to this update.
