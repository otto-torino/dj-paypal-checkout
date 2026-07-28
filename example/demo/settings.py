"""Settings for the dj-paypal-checkout demo.

Sandbox credentials come from the environment — never commit them:

    export PAYPAL_CLIENT_ID=...
    export PAYPAL_CLIENT_SECRET=...
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "demo-only-not-a-secret"
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "paypal_checkout",
    "shop",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "demo.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "static/"
USE_TZ = True

PAYPAL = {
    "CLIENT_ID": os.environ.get("PAYPAL_CLIENT_ID", ""),
    "CLIENT_SECRET": os.environ.get("PAYPAL_CLIENT_SECRET", ""),
    "LIVE": False,
    "CURRENCY": "EUR",
    # The posture the library is heading towards: on in every environment, so a
    # call site that forgets its idempotency key fails loudly instead of quietly.
    "STRICT_IDEMPOTENCY": True,
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "paypal_checkout": {"handlers": ["console"], "level": "INFO"},
        "shop": {"handlers": ["console"], "level": "INFO"},
    },
}
