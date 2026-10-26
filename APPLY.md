# TapTap update 3 — fix for random "Server Error (500)" (+ everything from updates 1 and 2)

## The problem
Redis (cache + Celery queue) briefly froze for over 1 second — mostly during its automatic background
snapshots — and Django's Redis cache turned every such freeze into a 500 on any page that used the cache
(chat polling, live ticks, Link check-ins …). A refresh a moment later worked again.

## The fix
- core/cache_backend.py      ResilientRedisCache: Redis trouble = "no cache" for a moment, never a 500;
                             circuit breaker keeps pages fast during a longer outage; warnings in the log
- config/settings.py         uses it; 2 s timeout, idle-connection health check; Celery no longer stores
                             task results nobody reads
- docker-compose.yml         Redis keeps the append-only file but drops the redundant snapshot forks

## Deploy (Redis must be recreated to pick up the new command)
    cd /opt/taptap
    unzip -o ~/taptap-update-3.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker live
    docker compose run --rm web python manage.py migrate
    docker compose up -d --force-recreate redis web worker live

## Check it worked
    docker compose exec redis redis-cli CONFIG GET save          # should show an empty value
    docker compose logs --since 24h web | grep -c " 500 "          # should drop to ~0
    docker compose logs --since 24h web | grep "Cache unavailable" # Redis hiccups now logged, not crashing
