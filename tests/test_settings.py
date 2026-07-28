from pathlib import Path

SECRET_KEY = "dummy-key-for-testing"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.admin",
    "paypal_checkout",
    "tests.test_app",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

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
        "NAME": ":memory:",
        # A *file* test database, not in-memory: the concurrency tests need two
        # connections to contend for real locks, and SQLite's shared-cache
        # in-memory mode returns "table is locked" immediately instead of
        # honouring the busy timeout.
        "TEST": {"NAME": str(Path(__file__).resolve().parent.parent / "test_db.sqlite3")},
        "OPTIONS": {"timeout": 20},
    },
}

USE_TZ = True

# Several tests deliberately trigger the "write without a request_id" warning.
# Send it to a null handler so it does not litter the test output; assertLogs
# still captures it, because it attaches its own handler to the logger.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "loggers": {"paypal_checkout": {"handlers": ["null"], "propagate": False}},
}

ROOT_URLCONF = "tests.urls"

# Sandbox-shaped dummy credentials. No test may perform a live call: every
# HTTP interaction is served from recorded fixtures.
PAYPAL = {
    "CLIENT_ID": "test-client-id",
    "CLIENT_SECRET": "test-client-secret",
    "LIVE": False,
    "WEBHOOK_ID": "test-webhook-id",
}
