#!/usr/bin/env bash
# Run the dj-paypal-checkout demo against the PayPal sandbox.
#
#   export PAYPAL_CLIENT_ID=...
#   export PAYPAL_CLIENT_SECRET=...
#   ./run_demo.sh
#
# Credentials come from a sandbox REST app: https://developer.paypal.com/dashboard/applications/sandbox
set -euo pipefail

cd "$(dirname "$0")"

if [[ -z "${PAYPAL_CLIENT_ID:-}" || -z "${PAYPAL_CLIENT_SECRET:-}" ]]; then
  echo "PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set (sandbox credentials)." >&2
  echo "Create a sandbox REST app at https://developer.paypal.com/dashboard/" >&2
  exit 1
fi

if [[ ! -d venv ]]; then
  echo "==> Creating venv"
  python3 -m venv venv
fi

echo "==> Installing the library and Django"
./venv/bin/python -m pip install --upgrade --quiet pip
./venv/bin/pip install --quiet -e .

echo "==> Migrating"
(cd example && "$PWD/../venv/bin/python" manage.py migrate --noinput)

echo "==> Creating the admin user (admin / password), if missing"
(cd example && "$PWD/../venv/bin/python" manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
User.objects.filter(username='admin').exists() or User.objects.create_superuser('admin', 'admin@example.com', 'password')
")

echo
echo "==> http://127.0.0.1:8000/         checkout"
echo "==> http://127.0.0.1:8000/admin/   admin / password"
echo
cd example && exec "$PWD/../venv/bin/python" manage.py runserver
