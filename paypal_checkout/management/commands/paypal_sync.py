"""Re-read orders from PayPal and settle whatever is still unconfirmed.

The intended use is a periodic job plus an on-demand tool for support:

    python manage.py paypal_sync --unconfirmed
    python manage.py paypal_sync --order 5O190127TN364715T
    python manage.py paypal_sync --unconfirmed --since 7 --dry-run
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from ...client import PayPalClient
from ...config import get_config
from ...exceptions import PayPalError
from ...models import Authorization, Capture, PayPalOrder, Refund
from ...orders import reconcile_order


class Command(BaseCommand):
    help = "Re-read PayPal orders and reconcile locally unconfirmed attempts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--order",
            dest="orders",
            action="append",
            default=[],
            metavar="PAYPAL_ID",
            help="Reconcile this PayPal order id. Repeatable.",
        )
        parser.add_argument(
            "--unconfirmed",
            action="store_true",
            help=(
                "Reconcile every order with an unconfirmed capture, authorization "
                "or refund."
            ),
        )
        parser.add_argument(
            "--since",
            type=int,
            default=None,
            metavar="DAYS",
            help="Only consider orders created in the last DAYS days.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Stop after this many orders (default 100).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be reconciled and change nothing.",
        )

    def handle(self, *args, **options):
        config = get_config()
        queryset = self.select(options)
        total = queryset.count()
        limit = options["limit"]
        orders = list(queryset[:limit])

        self.stdout.write(
            f"{total} order(s) to reconcile against {config.environment}"
            + (f"; taking the first {limit}" if total > limit else "")
        )
        if options["dry_run"]:
            for order in orders:
                self.stdout.write(f"  would reconcile {order.paypal_id} ({order.status})")
            self.stdout.write(self.style.WARNING("dry run: nothing changed"))
            return

        adopted = failed = unresolved = 0
        with PayPalClient(config) as client:
            for order in orders:
                try:
                    result = self.reconcile(client, order)
                except PayPalError as exc:
                    failed += 1
                    self.stderr.write(self.style.ERROR(f"  {order.paypal_id}: {exc}"))
                    continue
                adopted += len(result.get("adopted", []))
                unresolved += result.get("unresolved", 0)

        self.stdout.write(
            self.style.SUCCESS(
                f"done: {adopted} attempt(s) settled, {failed} failed, "
                f"{unresolved} unresolved refund(s)"
            )
        )
        if unresolved:
            raise CommandError(
                f"{unresolved} refund attempt(s) still need an explicit retry or review"
            )

    def reconcile(self, client, order):
        result = reconcile_order(client, order)
        line = f"  {result['order']} -> {result['status']}"
        for adopted in result.get("adopted", []):
            line += f"\n      settled {adopted}"
        for ambiguous in result.get("ambiguous", []):
            line += f"\n      {self.style.WARNING('ambiguous: ' + ambiguous)}"
        if result.get("unresolved"):
            warning = f"{result['unresolved']} unresolved refund(s)"
            line += f"\n      {self.style.WARNING(warning)}"
        self.stdout.write(line)
        return result

    def select(self, options):
        """Which orders to look at.

        Orders with no PayPal id are excluded on purpose: PayPal never confirmed
        the create call, so there is nothing to read back. They stay visible
        through ``PayPalOrder.objects.pending()``.
        """
        if not options["orders"] and not options["unconfirmed"]:
            raise CommandError("pass --order and/or --unconfirmed.")

        queryset = PayPalOrder.objects.exclude(paypal_id=None)
        filters = Q()
        if options["orders"]:
            filters |= Q(paypal_id__in=options["orders"])
        if options["unconfirmed"]:
            filters |= Q(captures__status=Capture.Status.INITIATED) | Q(
                authorizations__status=Authorization.Status.INITIATED
            ) | Q(
                captures__refunds__status__in=(
                    Refund.Status.INITIATED,
                    Refund.Status.UNRESOLVED,
                )
            )
        queryset = queryset.filter(filters).distinct()

        if options["since"] is not None:
            queryset = queryset.filter(
                created_at__gte=timezone.now() - timedelta(days=options["since"])
            )
        return queryset.order_by("created_at")
