import json
import re

from django.template import Context, Template
from django.test import SimpleTestCase, override_settings

from paypal_checkout.templatetags.paypal_checkout import (
    CONFIG_ELEMENT_ID,
    LIVE_SDK_HOST,
    SANDBOX_SDK_HOST,
    sdk_config,
)

SANDBOX = {"CLIENT_ID": "public-client-id", "CLIENT_SECRET": "SUPER-SECRET"}
LIVE = {**SANDBOX, "LIVE": True}


def render(source, **context):
    return Template("{% load paypal_checkout %}" + source).render(Context(context))


CONFIG_BLOCK = re.compile(
    rf'<script id="{CONFIG_ELEMENT_ID}" type="application/json">(.*?)</script>',
    re.DOTALL,
)


def parse_config(html):
    """Pull the JSON out of the block json_script wrote."""
    match = CONFIG_BLOCK.search(html)
    assert match, f"no {CONFIG_ELEMENT_ID} block in {html!r}"
    return json.loads(match.group(1))


@override_settings(PAYPAL=SANDBOX)
class SdkUrlTests(SimpleTestCase):
    def test_sandbox_url(self):
        self.assertEqual(render("{% paypal_sdk_url %}"), f"{SANDBOX_SDK_HOST}/web-sdk/v6/core")

    @override_settings(PAYPAL=LIVE)
    def test_live_url(self):
        self.assertEqual(render("{% paypal_sdk_url %}"), f"{LIVE_SDK_HOST}/web-sdk/v6/core")

    def test_v6_path_is_used(self):
        """v5 lived at /sdk/js; v6 is a different host path entirely."""
        self.assertIn("/web-sdk/v6/core", render("{% paypal_sdk_url %}"))


@override_settings(PAYPAL=SANDBOX)
class ClientIdTests(SimpleTestCase):
    def test_client_id_is_rendered(self):
        self.assertEqual(render("{% paypal_client_id %}"), "public-client-id")


@override_settings(PAYPAL=SANDBOX)
class PayPalSdkTagTests(SimpleTestCase):
    def test_script_and_config_are_emitted(self):
        html = render("{% paypal_sdk %}")

        self.assertIn(f'<script src="{SANDBOX_SDK_HOST}/web-sdk/v6/core"></script>', html)
        self.assertIn(f'id="{CONFIG_ELEMENT_ID}"', html)
        self.assertIn('type="application/json"', html)

    def test_config_contents(self):
        config = parse_config(render("{% paypal_sdk %}"))

        self.assertEqual(config["clientId"], "public-client-id")
        self.assertEqual(config["environment"], "sandbox")
        self.assertEqual(config["currency"], "EUR")
        self.assertEqual(config["components"], ["paypal-payments"])

    @override_settings(PAYPAL={**SANDBOX, "LIVE": True, "CURRENCY": "USD"})
    def test_config_follows_the_environment(self):
        config = parse_config(render("{% paypal_sdk %}"))

        self.assertEqual(config["environment"], "live")
        self.assertEqual(config["currency"], "USD")

    def test_components_can_be_listed(self):
        config = parse_config(render('{% paypal_sdk "paypal-payments, venmo-payments" %}'))

        self.assertEqual(config["components"], ["paypal-payments", "venmo-payments"])

    def test_blank_components_are_dropped(self):
        config = parse_config(render('{% paypal_sdk "paypal-payments,,  " %}'))

        self.assertEqual(config["components"], ["paypal-payments"])

    def test_the_secret_never_reaches_the_page(self):
        html = render("{% paypal_sdk %}{% paypal_client_id %}{% paypal_sdk_url %}")

        self.assertNotIn("SUPER-SECRET", html)

    def test_sdk_config_helper_is_importable_for_views(self):
        config = sdk_config()

        self.assertEqual(config["clientId"], "public-client-id")
        self.assertNotIn("clientSecret", config)
        self.assertNotIn("SUPER-SECRET", json.dumps(config))


@override_settings(PAYPAL=SANDBOX)
class InjectionTests(SimpleTestCase):
    """json_script must neutralise anything that could break out of the block."""

    @override_settings(
        PAYPAL={"CLIENT_ID": "</script><script>alert(1)</script>", "CLIENT_SECRET": "s"}
    )
    def test_a_hostile_client_id_cannot_break_out(self):
        html = render("{% paypal_sdk %}")

        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertEqual(parse_config(html)["clientId"], "</script><script>alert(1)</script>")
