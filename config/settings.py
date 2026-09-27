"""TapTap settings — every infrastructure choice comes from the environment so the
same image runs on a laptop, a LAN server over plain HTTP, or a cloud VPS behind
nginx / Traefik / Cloudflare, with or without Redis.
"""
from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    return os.getenv(name, '1' if default else '0').strip().lower() in {'1', 'true', 'yes', 'on'}


def env_list(name, default=''):
    return [x.strip() for x in os.getenv(name, default).split(',') if x.strip()]


SECRET_KEY = os.getenv('SECRET_KEY', 'dev-only-change-me')
DEBUG = env_bool('DEBUG', True)
ALLOWED_HOSTS = env_list('ALLOWED_HOSTS', 'localhost,127.0.0.1')

# CSRF: trust explicit origins plus every non-wildcard allowed host on http and https,
# so a reverse proxy, a LAN IP or a custom port does not break login forms.
CSRF_TRUSTED_ORIGINS = env_list('CSRF_TRUSTED_ORIGINS')
for _host in ALLOWED_HOSTS:
    if _host not in {'*', ''} and not _host.startswith('.'):
        for _scheme in ('https', 'http'):
            _origin = f'{_scheme}://{_host}'
            if _origin not in CSRF_TRUSTED_ORIGINS:
                CSRF_TRUSTED_ORIGINS.append(_origin)

INSTALLED_APPS = ['django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions',
                  'django.contrib.messages', 'django.contrib.staticfiles', 'core']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'whitenoise.middleware.WhiteNoiseMiddleware',
              'django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.common.CommonMiddleware',
              'django.middleware.csrf.CsrfViewMiddleware', 'django.contrib.auth.middleware.AuthenticationMiddleware',
              'django.contrib.messages.middleware.MessageMiddleware', 'django.middleware.clickjacking.XFrameOptionsMiddleware',
              'core.middleware.SubscriptionAccessMiddleware']
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'DIRS': [BASE_DIR / 'templates'], 'APP_DIRS': True,
              'OPTIONS': {'context_processors': ['django.template.context_processors.request', 'django.contrib.auth.context_processors.auth',
                                                 'django.contrib.messages.context_processors.messages', 'core.context_processors.business_context']}}]
WSGI_APPLICATION = 'config.wsgi.application'

# Database: PostgreSQL by default; DB_ENGINE=sqlite for a single small box.
if os.getenv('DB_ENGINE', 'postgres').lower() == 'sqlite':
    # WAL + immediate transactions + busy timeout: parallel discovery requests
    # queue for the write lock instead of failing with "database is locked".
    DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': os.getenv('SQLITE_PATH', str(BASE_DIR / 'db.sqlite3')),
                             'OPTIONS': {'timeout': 30, 'transaction_mode': 'IMMEDIATE',
                                         'init_command': 'PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;'}}}
else:
    DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql', 'NAME': os.getenv('POSTGRES_DB', 'taptap'),
                             'USER': os.getenv('POSTGRES_USER', 'taptap'), 'PASSWORD': os.getenv('POSTGRES_PASSWORD', 'taptap'),
                             'HOST': os.getenv('POSTGRES_HOST', 'db'), 'PORT': os.getenv('POSTGRES_PORT', '5432'),
                             'CONN_MAX_AGE': int(os.getenv('DB_CONN_MAX_AGE', '60')), 'CONN_HEALTH_CHECKS': True}}

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = 'en-us'; TIME_ZONE = os.getenv('TIME_ZONE', 'Africa/Banjul'); USE_I18N = True; USE_TZ = True
STATIC_URL = os.getenv('STATIC_URL', '/static/'); STATIC_ROOT = BASE_DIR / 'staticfiles'; STATICFILES_DIRS = [BASE_DIR / 'static']
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LOGIN_URL = 'login'; LOGIN_REDIRECT_URL = 'dashboard'; LOGOUT_REDIRECT_URL = 'home'

# Cookies: secure by default in production, but SECURE_COOKIES=0 lets a plain-HTTP
# LAN deployment log in (secure cookies are silently dropped over http).
SECURE_COOKIES = env_bool('SECURE_COOKIES', not DEBUG)
CSRF_COOKIE_SECURE = SECURE_COOKIES; SESSION_COOKIE_SECURE = SECURE_COOKIES
# Behind a reverse proxy (nginx, Traefik, Caddy, Cloudflare Tunnel).
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = env_bool('USE_X_FORWARDED_HOST', True)
USE_X_FORWARDED_PORT = env_bool('USE_X_FORWARDED_PORT', False)

TRIAL_DAYS = int(os.getenv('TRIAL_DAYS', '7'))
MIKROTIK_API_PORT = int(os.getenv('MIKROTIK_API_PORT', '8728'))
# Router connection behaviour — bounded so a dead router never hangs a page.
MIKROTIK_TIMEOUT = float(os.getenv('MIKROTIK_TIMEOUT', '10'))          # discovery / config reads
MIKROTIK_LIVE_TIMEOUT = float(os.getenv('MIKROTIK_LIVE_TIMEOUT', '5'))  # live traffic polls
MIKROTIK_SSL_VERIFY = env_bool('MIKROTIK_SSL_VERIFY', False)            # api-ssl uses self-signed certs by default
LIVE_CACHE_SECONDS = int(os.getenv('LIVE_CACHE_SECONDS', '2'))
TOPOLOGY_STALE_SECONDS = int(os.getenv('TOPOLOGY_STALE_SECONDS', '120'))

# Background synchronization
CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'redis://redis:6379/0')
CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'redis://redis:6379/1')
CELERY_TASK_ALWAYS_EAGER = env_bool('CELERY_EAGER', False)  # no Redis/worker? run syncs inline
CELERY_TASK_TRACK_STARTED = True
# Live sync: how often routers are checked for new/used vouchers, sessions and IP bindings.
LIVE_WATCH_SECONDS = int(os.getenv('LIVE_WATCH_SECONDS', '15'))
PRESENCE_SECONDS = int(os.getenv('PRESENCE_SECONDS', '60'))          # device online/offline scan
TRAFFIC_APP_SECONDS = int(os.getenv('TRAFFIC_APP_SECONDS', '60'))    # app/site sampling from the connection table
TRAFFIC_MAX_CONNECTIONS = int(os.getenv('TRAFFIC_MAX_CONNECTIONS', '40000'))
CELERY_BEAT_SCHEDULE = {
    'taptap-live-watch': {'task': 'core.tasks.live_watch_all', 'schedule': float(LIVE_WATCH_SECONDS),
                          'options': {'expires': LIVE_WATCH_SECONDS * 2}},
}
# Live checks run on their own queue so they never wait behind a long full sync.
CELERY_TASK_ROUTES = {'core.tasks.live_watch_all': {'queue': 'live'}}
CELERY_TASK_TIME_LIMIT = int(os.getenv('CELERY_TASK_TIME_LIMIT', '1800'))
CELERY_TASK_SOFT_TIME_LIMIT = int(os.getenv('CELERY_TASK_SOFT_TIME_LIMIT', '1700'))
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True

# Shared cache for live telemetry (so every gunicorn worker/thread reuses one router read).
_cache_url = os.getenv('CACHE_URL', '')
if not _cache_url and CELERY_BROKER_URL.startswith('redis://') and not CELERY_TASK_ALWAYS_EAGER:
    _cache_url = CELERY_BROKER_URL.rsplit('/', 1)[0] + '/2'
if _cache_url.startswith(('redis://', 'rediss://')):
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.redis.RedisCache', 'LOCATION': _cache_url,
                          'OPTIONS': {'socket_connect_timeout': 1, 'socket_timeout': 1}}}
else:
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'taptap'}}

LOGGING = {'version': 1, 'disable_existing_loggers': False,
           'handlers': {'console': {'class': 'logging.StreamHandler'}},
           'root': {'handlers': ['console'], 'level': os.getenv('LOG_LEVEL', 'INFO')}}


# ─── Email (notifications) ───────────────────────────────────────────────
# Without EMAIL_HOST emails are only written to the log (console backend).
EMAIL_HOST = os.getenv('EMAIL_HOST', '')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', EMAIL_PORT == 587)
EMAIL_USE_SSL = env_bool('EMAIL_USE_SSL', EMAIL_PORT == 465)
EMAIL_TIMEOUT = int(os.getenv('EMAIL_TIMEOUT', '15'))
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', EMAIL_HOST_USER or 'TapTap <no-reply@localhost>')
EMAIL_BACKEND = os.getenv('EMAIL_BACKEND', 'django.core.mail.backends.smtp.EmailBackend' if EMAIL_HOST else 'django.core.mail.backends.console.EmailBackend')

# Public address of this TapTap (used in emails and in the TapTap Link router script).
SITE_URL = os.getenv('SITE_URL', '').rstrip('/')
# TapTap Link: routers verify the server certificate (turn off only for testing with self-signed certificates).
AGENT_VERIFY_TLS = env_bool('AGENT_VERIFY_TLS', True)
# TapTap Link routers re-upload their full inventory this often (minutes).
LINK_SYNC_MINUTES = int(os.getenv('LINK_SYNC_MINUTES', '30'))

CELERY_BEAT_SCHEDULE['taptap-notifications'] = {'task': 'core.tasks.deliver_notifications', 'schedule': 60.0, 'options': {'expires': 120}}
CELERY_TASK_ROUTES['core.tasks.deliver_notifications'] = {'queue': 'live'}
