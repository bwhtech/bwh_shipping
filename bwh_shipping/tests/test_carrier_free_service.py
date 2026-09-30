# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from bwh_shipping.bwh_shipping.pricing import get_enabled_services, quote_services


class TestCarrierFreeService(IntegrationTestCase):
	"""A delivery option the store delivers itself: no provider, priced by its rule or backup charge."""

	# Title is the autoname and the run rolls back per class, not per test, so each case needs its own.

	def test_saves_with_only_a_backup_charge(self):
		service = make_carrier_free_service("Own Van", backup_charge=75).insert()

		self.assertFalse(service.provider)

	def test_throws_when_nothing_prices_it(self):
		with self.assertRaises(frappe.ValidationError):
			make_carrier_free_service("Unpriced Van").insert()

	def test_a_disabled_one_needs_no_price_yet(self):
		service = make_carrier_free_service("Draft Van", enabled=0).insert()

		self.assertFalse(service.enabled)

	def test_checkout_quotes_it_at_its_backup_charge(self):
		service = make_carrier_free_service("Quoted Van", backup_charge=75).insert()
		get_enabled_services.clear_cache()

		# Other enabled options on the site belong to real carriers; their live quote would leave the box
		# and its request log commits mid-test.
		with patch("bwh_shipping.bwh_shipping.pricing.get_live_quotes", return_value={}):
			options = quote_services(
				None, {"country": "India"}, [{"weight": 1, "count": 1}], {"base_net_total": 500}
			)
		quoted = next(option for option in options if option["title"] == service.title)

		self.assertEqual(quoted["amount"], 75)
		self.assertFalse(quoted["provider"])
		self.assertFalse(quoted["is_live_rate"])


def make_carrier_free_service(title: str, backup_charge: float = 0, enabled: int = 1):
	service = frappe.new_doc("Shipping Service")
	service.update(
		{
			"title": f"_Test {title} {frappe.generate_hash(length=6)}",
			"enabled": enabled,
			"backup_charge": backup_charge,
		}
	)
	return service
