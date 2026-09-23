"""
Django settings for the Lead Management CRM.

Dev default: SQLite (zero setup, works out of the box).
Production: set USE_MYSQL=1 and the DB_* / SECRET_KEY / DEBUG env vars —
see .env.example and README.md for the full production checklist
(Nginx, Gunicorn, HTTPS, Celery/Redis are documented there, not wired up
in this MVP).
"""

import os
from pathlib import Path 

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env file if present
env_file = BASE_DIR / '.env'
if env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file, override=True)
    except ImportError:
        with open(env_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ[k.strip()] = v.strip()

SECRET_KEY = os.environ.get("SECRET_KEY", "django-insecure-dev-key-change-me-in-production")

DEBUG = os.environ.get("DEBUG", "1") == "1"

ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',

    'corsheaders',
    'rest_framework',
    'rest_framework_simplejwt',

    'accounts',
    'leads',
    'followups',
    'imports',
    'admissions',
    'payments',
    'dashboard',
    'audit',
    'meta_ads',
    'api',
    'notifications',
    'anymail',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'audit.middleware.CurrentUserMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'accounts.context_processors.global_business_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

AUTH_USER_MODEL = 'accounts.User'

USE_MYSQL = os.environ.get("USE_MYSQL", "0") in ("1", "true", "True")

if USE_MYSQL:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.mysql',
            'NAME': os.environ.get('DB_NAME', 'leadcrm'),
            'USER': os.environ.get('DB_USER', 'root'),
            'PASSWORD': os.environ.get('DB_PASSWORD', ''),
            'HOST': os.environ.get('DB_HOST', '127.0.0.1'),
            'PORT': os.environ.get('DB_PORT', '3306'),
            'CONN_MAX_AGE': 60,
            'OPTIONS': {
                'charset': 'utf8mb4',
                'connect_timeout': 60,
                'read_timeout': 300,
                'write_timeout': 300,
                'init_command': (
                    "SET sql_mode='STRICT_TRANS_TABLES', "
                    "net_read_timeout=300, "
                    "net_write_timeout=300, "
                    "wait_timeout=600, "
                    "interactive_timeout=600"
                ),
            },
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = os.environ.get("TIME_ZONE", "Asia/Kolkata")
USE_I18N = True
USE_TZ = True

# Date and Datetime formatting defaults (DD-MM-YYYY)
DATE_FORMAT = 'd-m-Y'
SHORT_DATE_FORMAT = 'd-m-Y'
DATETIME_FORMAT = 'd-m-Y H:i'
SHORT_DATETIME_FORMAT = 'd-m-Y H:i'

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static'] if (BASE_DIR / 'static').exists() else []
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'accounts:login'
LOGIN_REDIRECT_URL = 'dashboard:home'
LOGOUT_REDIRECT_URL = 'accounts:login'

# Excel import file size / row safety limits
MAX_IMPORT_FILE_MB = 15
MAX_IMPORT_ROWS = 50000



# Cache Configuration (Persistent across all WSGI/Gunicorn workers)
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
        'LOCATION': BASE_DIR / '.django_cache',
        'TIMEOUT': 86400,  # 24 hours
        'OPTIONS': {
            'MAX_ENTRIES': 10000,
        }
    }
}

# Session & Cookie Persistence Settings (30 Days Long-Lived Session)
SESSION_ENGINE = 'django.contrib.sessions.backends.db'
SESSION_COOKIE_NAME = 'sessionid_crm_dev' if DEBUG else 'sessionid_crm'
SESSION_COOKIE_AGE = 2592000  # 30 Days (2592000 seconds)
SESSION_SAVE_EVERY_REQUEST = True  # Automatically refresh session timer on active requests
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_HTTPONLY = False
CSRF_USE_SESSIONS = False
CSRF_TRUSTED_ORIGINS = [
    'https://crm.zappcode.in',
    'http://crm.zappcode.in',
    'http://localhost:8000',
    'http://127.0.0.1:8000',
]

# Proxy SSL Header for Nginx/Production HTTPS
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

if not DEBUG:
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    # If site is behind reverse proxy (Nginx), allow non-HTTPS cookie fallback if proxy terminates SSL
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False

# Email Configuration (Brevo API via Anymail)
EMAIL_BACKEND = os.environ.get('EMAIL_BACKEND', 'anymail.backends.brevo.EmailBackend')
ANYMAIL = {
    "BREVO_API_KEY": os.environ.get('BREVO_API_KEY', os.environ.get('EMAIL_HOST_PASSWORD', '')),
}
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'Zappkode CRM <zappkodesolutions@gmail.com>')
SERVER_EMAIL = DEFAULT_FROM_EMAIL



# REST Framework & JWT Configuration
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
}

from datetime import timedelta
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(days=1),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "AUTH_HEADER_TYPES": ("Bearer",),
}

CORS_ALLOW_ALL_ORIGINS = True


# ─── Log directory ──────────────────────────────────────────────────────────
LOGS_DIR = BASE_DIR / 'logs'
LOGS_DIR.mkdir(exist_ok=True)

# Logging Configuration — rotating file so it never grows unbounded
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOGS_DIR / 'django.log',
            'maxBytes': 5 * 1024 * 1024,  # 5 MB per file
            'backupCount': 3,              # keep last 3 rotated files
            'formatter': 'verbose',
            'encoding': 'utf-8',
        },
        'console': {
            'level': 'INFO',
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
    },
    'loggers': {
        'django': {
            'handlers': ['file', 'console'],
            'level': 'INFO',
            'propagate': True,
        },
        'django.security.DisallowedHost': {
            'handlers': [],         # silence noisy bot requests
            'propagate': False,
        },
        'api': {
            'handlers': ['file', 'console'],
            'level': 'INFO',
            'propagate': False,
        },
        'meta_ads': {
            'handlers': ['file', 'console'],
            'level': 'INFO',
            'propagate': False,
        },
        'leads': {
            'handlers': ['file', 'console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}

# ---------------------------------------------------------------------------
# Celery Configuration
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/0")
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = os.environ.get("TIME_ZONE", "Asia/Kolkata")
CELERY_ENABLE_UTC = False

# Celery Beat Schedule (Runs every 3 minutes to auto-sync Meta leads)
CELERY_SYNC_INTERVAL = float(os.environ.get("CELERY_SYNC_INTERVAL", 180.0))  # Default 3 minutes (180s)
CELERY_BEAT_SCHEDULE = {
    "sync-meta-leads-periodic": {
        "task": "meta_ads.tasks.sync_meta_leads_task",
        "schedule": CELERY_SYNC_INTERVAL,
    },
}

