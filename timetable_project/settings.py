"""
Django settings for timetable_project project.
"""

import os  # FIX BUG 1: moved to top — os.path.join() is used at module level for MEDIA_ROOT
import sys

# Configure Windows DLL search paths for C++ runtime and OR-Tools dependencies
if sys.platform == 'win32':
    for _dll_path in [
        r'C:\Program Files\Mozilla Firefox',
        os.path.join(os.path.dirname(os.path.dirname(__file__)), '.venv', 'Lib', 'site-packages', 'ortools', '.libs'),
    ]:
        if os.path.exists(_dll_path):
            try:
                os.add_dll_directory(_dll_path)
            except Exception:
                pass

# Monkey patch pyparsing for older httplib2 versions
import pyparsing
pyparsing.DelimitedList = pyparsing.delimitedList

from pathlib import Path
from decouple import config, Csv
import dj_database_url

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# ── Security ─────────────────────────────────────────────────────────────────
SECRET_KEY = config('SECRET_KEY')
DEBUG = config('DEBUG', default=False, cast=bool)
if DEBUG:
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='127.0.0.1,localhost', cast=Csv())
CSRF_TRUSTED_ORIGINS = config(
    'CSRF_TRUSTED_ORIGINS',
    default='http://127.0.0.1,http://localhost,http://167.233.37.95,https://167.233.37.95',
    cast=Csv()
)

# ── Application definition ───────────────────────────────────────────────────
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third-party
    'django_q',
    'rest_framework',
    # Local
    'accounts',
    'scheduler',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'timetable_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'scheduler.context_processors.active_scheduler_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'timetable_project.wsgi.application'

DATABASE_URL = config('DATABASE_URL', default=None)
if DATABASE_URL:
    DATABASES = {
        'default': dj_database_url.config(
            default=DATABASE_URL,
            conn_max_age=0 if 'sqlite' in DATABASE_URL else 600
        )
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
            'OPTIONS': {
                'timeout': 60,
                'init_command': (
                    'PRAGMA journal_mode=WAL;'
                    'PRAGMA synchronous=NORMAL;'
                    'PRAGMA busy_timeout=60000;'
                ),
            }
        }
    }

if DATABASES['default']['ENGINE'] == 'django.db.backends.sqlite3':
    DATABASES['default'].setdefault('OPTIONS', {})['timeout'] = 60


# ── Password validation ───────────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# FIX BUG 4: Removed MD5PasswordHasher — it is cryptographically broken and a security risk.
# PBKDF2 (Django's default) is the only hasher needed. If legacy MD5 passwords exist in the
# database, they will be automatically upgraded to PBKDF2 on next login.
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.PBKDF2PasswordHasher',
    'django.contrib.auth.hashers.Argon2PasswordHasher',
    'django.contrib.auth.hashers.BCryptSHA256PasswordHasher',
]



# ── Auth redirects ────────────────────────────────────────────────────────────
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/accounts/login/'

AUTHENTICATION_BACKENDS = [
    'accounts.backends.EmailOrUsernameModelBackend',
    'django.contrib.auth.backends.ModelBackend',
]

# ── Internationalization ──────────────────────────────────────────────────────
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Nairobi'
USE_I18N = True
USE_TZ = True

# ── Static files ──────────────────────────────────────────────────────────────
STATIC_URL = 'static/'
# FIX BUG 7: STATIC_ROOT is required for `collectstatic` (production deployment via Docker/Nginx).
STATIC_ROOT = BASE_DIR / 'staticfiles'

# ── Default primary key ───────────────────────────────────────────────────────
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── Redis Cache ───────────────────────────────────────────────────────────────
# REDIS_URL can be set in .env (e.g. redis://127.0.0.1:6379/1).
# Falls back to Django's in-memory LocMemCache when Redis is not configured
# (safe for local dev without Redis installed).
REDIS_URL = config('REDIS_URL', default=None)

if REDIS_URL:
    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': REDIS_URL,
            'OPTIONS': {
                'CLIENT_CLASS': 'django_redis.client.DefaultClient',
                # Silently return cache misses instead of raising an error
                # if Redis goes down — app degrades gracefully.
                'IGNORE_EXCEPTIONS': True,
                # Compress values larger than 10KB (useful for large solver payloads)
                'COMPRESSOR': 'django_redis.compressors.zlib.ZlibCompressor',
                'CONNECTION_POOL_KWARGS': {
                    'max_connections': 50,
                },
            },
            'KEY_PREFIX': 'timetable',
            'TIMEOUT': 300,  # default TTL: 5 minutes
        }
    }
    # Store Django sessions in Redis for fast, centralized session management.
    # This is especially useful in multi-worker / Docker deployments.
    SESSION_ENGINE = 'django.contrib.sessions.backends.cache'
    SESSION_CACHE_ALIAS = 'default'
else:
    # No Redis configured: use Django's thread-safe in-memory cache.
    # Per-process only — fine for single-worker local dev.
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'timetable-locmem',
        }
    }

# ── Email ─────────────────────────────────────────────────────────────────────
EMAIL_BACKEND = config('EMAIL_BACKEND', default='django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = config('EMAIL_HOST', default='smtp.gmail.com')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_USE_SSL = config('EMAIL_USE_SSL', default=False, cast=bool)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='Timetable System <no-reply@timetable.edu>')
EMAIL_TIMEOUT = 10  # seconds — hard ceiling for any synchronous SMTP call

# ── Django-Q2 (async task queue) ─────────────────────────────────────────────
# When REDIS_URL is set, use Redis as the broker for better performance:
#   - Tasks are delivered over TCP (no DB polling latency)
#   - Redis pub/sub lets the worker wake up instantly on new tasks
#   - Avoids SQLite WAL contention under heavy generation load
# Falls back to the ORM broker (no extra dep needed) when Redis is absent.
_q_broker = {}
if REDIS_URL:
    _q_broker = {'redis': REDIS_URL}
else:
    _q_broker = {'orm': 'default'}  # ORM broker (SQLite-safe fallback)

Q_CLUSTER = {
    'name': 'timetable_scheduler',
    **_q_broker,
    'workers': 1,            # 1 worker — avoids MemoryError on machines with <2 GB
                             # free RAM; timetable generation is sequential anyway
    'recycle': 500,
    'timeout': 300,          # 5 minutes max per task
    'retry': 360,
    'queue_limit': 50,
    'bulk': 10,
    'prefetch': 1,           # only pull 1 task at a time; no point buffering more
                             # than the single worker can handle
    'sync': config('Q_CLUSTER_SYNC', default=DEBUG, cast=bool),  # True = synchronous for testing
    # ── Result-table hygiene (applies to ORM broker; ignored for Redis broker) ────
    'save_limit': 250,       # keep only the 250 most recent task results
    'compress': True,        # zlib-compress serialised task payloads
    'ack_failures': True,    # mark crashed/exception tasks as FAILED immediately
}

# ── Django REST Framework ─────────────────────────────────────────────────────
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
        'rest_framework.authentication.BasicAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticatedOrReadOnly',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
}

# ── Firebase Configuration ───────────────────────────────────────────────────
FIREBASE_CREDENTIALS_JSON = config('FIREBASE_CREDENTIALS_JSON', default=None)
FIREBASE_DATABASE_URL = config('FIREBASE_DATABASE_URL', default=None)

FIREBASE_CONFIG = {
    'apiKey': config('FIREBASE_API_KEY', default=None),
    'authDomain': config('FIREBASE_AUTH_DOMAIN', default=None),
    'projectId': config('FIREBASE_PROJECT_ID', default=None),
    'storageBucket': config('FIREBASE_STORAGE_BUCKET', default=None),
    'messagingSenderId': config('FIREBASE_MESSAGING_SENDER_ID', default=None),
    'appId': config('FIREBASE_APP_ID', default=None),
    'databaseURL': FIREBASE_DATABASE_URL,
}
# Trigger reload

# ── Media Files ──────────────────────────────────────────────────────────────
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')
