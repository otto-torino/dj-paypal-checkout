# Configuration file for the Sphinx documentation builder.
#
# https://www.sphinx-doc.org/en/master/usage/configuration.html

import os
import sys

import django

sys.path.insert(0, os.path.abspath("../.."))

# Minimal Django setup so autodoc can import the package.
from django.conf import settings  # noqa: E402

settings.configure(
    INSTALLED_APPS=[
        "django.contrib.admin",
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "django.contrib.sessions",
        "django.contrib.messages",
        "paypal_checkout",
    ],
    DATABASES={},
    USE_TZ=True,
)
django.setup()

import paypal_checkout  # noqa: E402

# -- Project information -----------------------------------------------------

project = "dj-paypal-checkout"
copyright = "2026, Otto"
author = "Otto"
release = paypal_checkout.__version__

# -- General configuration ---------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.viewcode",
    "sphinx.ext.napoleon",
]

templates_path = ["_templates"]
exclude_patterns = []

# -- Options for HTML output -------------------------------------------------

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]
