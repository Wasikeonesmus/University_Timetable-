"""
Django settings for timetable_project project.
"""

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
    import os
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='127.0.0.1,localhost', cast=Csv())

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
            conn_max_age=600
        )
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
            'OPTIONS': {
                'timeout': 30,
            }
        }
    }

# ── Password validation ───────────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

import sys
if 'test' in sys.argv or 'test_coverage' in sys.argv or any('profile_tests' in arg for arg in sys.argv):
    PASSWORD_HASHERS = [
        'django.contrib.auth.hashers.MD5PasswordHasher',
    ]



# ── Auth redirects ────────────────────────────────────────────────────────────
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/accounts/login/'

# ── Internationalization ──────────────────────────────────────────────────────
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Nairobi'
USE_I18N = True
USE_TZ = True

# ── Static files ──────────────────────────────────────────────────────────────
STATIC_URL = 'static/'

# ── Default primary key ───────────────────────────────────────────────────────
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── Email ─────────────────────────────────────────────────────────────────────
EMAIL_BACKEND = config('EMAIL_BACKEND', default='django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = config('EMAIL_HOST', default='smtp.gmail.com')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='Timetable System <no-reply@timetable.edu>')

# ── Django-Q2 (async task queue) ─────────────────────────────────────────────
Q_CLUSTER = {
    'name': 'timetable_scheduler',
    'workers': 2,
    'recycle': 500,
    'timeout': 300,          # 5 minutes max per task
    'retry': 360,
    'queue_limit': 50,
    'bulk': 10,
    'orm': 'default',        # Use Django ORM as broker (no Redis needed)
    'sync': False,           # True = synchronous for testing
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
