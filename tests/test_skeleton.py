"""M0 smoke tests: the package installs, the app loads, versions agree.

Thin by design — the real suites (config, client, orders, webhooks) arrive
with their milestones.
"""

import tomllib
from pathlib import Path

from django.apps import apps
from django.test import TestCase

import paypal_checkout

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


class SkeletonTests(TestCase):
    def test_app_is_installed(self):
        config = apps.get_app_config("paypal_checkout")
        self.assertEqual(config.name, "paypal_checkout")

    def test_version_matches_pyproject(self):
        """Releases are triggered by the version in pyproject.toml, so a
        mismatch with `__version__` would ship a wrongly-labelled package."""
        with PYPROJECT.open("rb") as fh:
            declared = tomllib.load(fh)["project"]["version"]
        self.assertEqual(paypal_checkout.__version__, declared)

    def test_test_app_model_is_usable(self):
        from tests.test_app.models import ShopOrder

        order = ShopOrder.objects.create(reference="ORD-1", total="10.00")
        self.assertEqual(str(order), "ORD-1")
