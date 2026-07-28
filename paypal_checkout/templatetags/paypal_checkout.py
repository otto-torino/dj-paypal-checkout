"""Template tags for the PayPal JS SDK v6.

These only ever emit **public** values: the client id (which is public by
design), the environment and the currency. The secret never reaches a template —
a test asserts as much.

The tags deliberately stop at loading the SDK and exposing its configuration.
Wiring buttons to your own create/capture endpoints is application code — URLs,
CSRF and error handling differ per project — and the ``example/`` demo shows one
complete way to do it.
"""

from django import template

from ..config import get_config

register = template.Library()

SANDBOX_SDK_HOST = "https://www.sandbox.paypal.com"
LIVE_SDK_HOST = "https://www.paypal.com"
SDK_PATH = "/web-sdk/v6/core"

#: Element id of the JSON block written by :func:`paypal_sdk`.
CONFIG_ELEMENT_ID = "paypal-sdk-config"

DEFAULT_COMPONENTS = "paypal-payments"


def _split_components(components):
    return [part.strip() for part in str(components).split(",") if part.strip()]


@register.simple_tag
def paypal_sdk_url(config=None):
    """URL of the v6 SDK for the configured environment.

    Sandbox and live are different hosts, which is an easy thing to get wrong by
    hand — hence a tag rather than a hardcoded ``<script src>``.
    """
    config = config or get_config()
    host = LIVE_SDK_HOST if config.live else SANDBOX_SDK_HOST
    return f"{host}{SDK_PATH}"


@register.simple_tag
def paypal_client_id(config=None):
    """The public client id, for templates that build their own script tag."""
    return (config or get_config()).client_id


def sdk_config(components=DEFAULT_COMPONENTS, config=None):
    """The public SDK configuration as a dict.

    Not a template tag on purpose: a tag returning a JSON *string* would be
    HTML-escaped by autoescaping, and marking it safe inside a ``<script>`` is
    the very hole ``json_script`` exists to close. Use :func:`paypal_sdk` in
    templates, and this function when you need to serve the configuration from
    a view.
    """
    config = config or get_config()
    return {
        "clientId": config.client_id,
        "environment": config.environment,
        "currency": config.currency,
        "components": _split_components(components),
    }


@register.inclusion_tag("paypal_checkout/sdk.html")
def paypal_sdk(components=DEFAULT_COMPONENTS, config=None):
    """Load the SDK and publish its configuration as JSON.

    Renders the ``<script src>`` for the right environment plus a
    ``<script type="application/json" id="paypal-sdk-config">`` block, so your
    JavaScript can read the client id and currency without any of it being
    templated into executable code::

        {% load paypal_checkout %}
        {% paypal_sdk %}

        <script>
          const cfg = JSON.parse(
            document.getElementById("paypal-sdk-config").textContent);
          const sdk = await window.paypal.createInstance({
            clientId: cfg.clientId, components: cfg.components,
          });
        </script>
    """
    config = config or get_config()
    return {
        "sdk_url": paypal_sdk_url(config),
        "sdk_config": sdk_config(components=components, config=config),
        "config_element_id": CONFIG_ELEMENT_ID,
    }
