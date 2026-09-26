#!/bin/sh
set -e
until nc -z ${POSTGRES_HOST:-db} ${POSTGRES_PORT:-5432}; do echo "Waiting for database..."; sleep 1; done
python manage.py migrate --noinput
python manage.py collectstatic --noinput
exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 90
