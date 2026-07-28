SECRET_KEY = "dummy-key-for-testing"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "paypal_checkout",
    "tests.test_app",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
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
