Installation
============

.. note::

   Not published on PyPI yet — install from the repository until 0.1.0.

.. code-block:: bash

   pip install dj-paypal-checkout

Offline webhook signature verification requires the optional ``crypto``
extra:

.. code-block:: bash

   pip install "dj-paypal-checkout[crypto]"

Add the app to ``INSTALLED_APPS``:

.. code-block:: python

   INSTALLED_APPS = [
       ...
       "django.contrib.contenttypes",  # required: models use a generic FK
       "paypal_checkout",
   ]

Then run the migrations:

.. code-block:: bash

   python manage.py migrate paypal_checkout

Requirements
------------

* Python 3.11+
* Django 5.2 LTS or 6.0
* ``httpx``
