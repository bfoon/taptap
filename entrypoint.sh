#!/bin/sh
set -e
if [ "${DB_ENGINE:-postgres}" != "sqlite" ]; then
  until nc -z ${POSTGRES_HOST:-db} ${POSTGRES_PORT:-5432}; do echo "Waiting for database..."; sleep 1; done
fi
python manage.py migrate --noinput
python manage.py collectstatic --noinput
# gthread workers: live-telemetry polls wait on routers (I/O), so threads keep
# the site responsive while a slow router answers.
exec gunicorn config.wsgi:application \
  --bind 0.0.0.0:${PORT:-8000} \
  --worker-class gthread \
  --workers ${GUNICORN_WORKERS:-3} \
  --threads ${GUNICORN_THREADS:-8} \
  --timeout ${GUNICORN_TIMEOUT:-120} \
  --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-*}" \
  --access-logfile -
