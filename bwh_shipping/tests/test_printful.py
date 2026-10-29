# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

import json
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from bwh_shipping.bwh_shipping.doctype.printful_shipping_settings.printful_shipping_settings import (
	PrintfulShippingSettings,
	state_code,
)
from bwh_shipping.exceptions import PartialBookingError

SETTINGS_DOCTYPE = "Printful Shipping Settings"

DESTINATION = {
	"contact_name": "Jane Doe",
	"line1": "19749 Dearborn St",
	"city": "Chatsworth",
	"state": "California",
	"pincode": "91311",
	"country": "United States",
}

PARCELS = [{"weight": 0.5, "items": [{"sku": "TEE-BLK-M", "description": "Tee", "quantity": 2, "rate": 25}]}]

SHIPPED_ORDER = {
	"id": 1001,
	"status": "fulfilled",
	"shipments": [
		{
			"id": 7,
			"carrier": "USPS",
			"service": "Ground",
			"tracking_number": "9400100000000000000000",
			"tracking_url": "https://track.example.com/9400100000000000000000",
			"shipped_at": "2026-10-20 10:00:00",
		}
	],
}


class PrintfulTestCase(IntegrationTestCase):
	def setUp(self):
		self.settings = frappe.get_single(SETTINGS_DOCTYPE)
		self.settings.test_mode = 0
		self.settings.currency = "USD"
		self.calls = []

	def stub_printful(self, responses: dict):
		"""Neutralise only the outbound boundary: each (method, path) answers from `responses`."""

		def request(settings, method, path, payload=None, timeout=None):
			self.calls.append((method, path, payload))
			response = responses[(method, path)]
			if isinstance(response, Exception):
				raise response
			return response

		return patch.object(PrintfulShippingSettings, "request", request)


class TestPrintfulRates(PrintfulTestCase):
	def test_rates_are_quoted_from_the_items(self):
		rates = [
			{"id": "STANDARD", "name": "Flat Rate", "rate": "4.99", "currency": "USD", "maxDeliveryDays": 5}
		]
		with self.stub_printful({("POST", "/shipping/rates"): rates}):
			quoted = self.settings.get_rates({}, DESTINATION, PARCELS)

		payload = self.calls[0][2]
		self.assertEqual(
			payload["items"], [{"external_variant_id": "TEE-BLK-M", "quantity": 2, "value": "25.0"}]
		)
		self.assertEqual(payload["recipient"]["country_code"], "US")
		self.assertEqual(quoted[0]["service_code"], "STANDARD")
		self.assertEqual(quoted[0]["amount"], 4.99)
		self.assertEqual(quoted[0]["transit_days"], 5)

	def test_a_cart_without_items_is_not_quoted(self):
		with self.stub_printful({}):
			self.assertEqual(self.settings.get_rates({}, DESTINATION, [{"weight": 1}]), [])
		self.assertEqual(self.calls, [])


class TestPrintfulBooking(PrintfulTestCase):
	def shipment(self):
		return {
			"reference": "SHIP-26-00001",
			"service_code": "STANDARD",
			"destination": DESTINATION,
			"parcels": PARCELS,
		}

	def test_draft_is_created_then_confirmed(self):
		draft = {"id": 1001, "status": "draft"}
		confirmed = {"id": 1001, "status": "pending", "costs": {"currency": "USD", "shipping": "4.99"}}
		with self.stub_printful({("POST", "/orders"): draft, ("POST", "/orders/1001/confirm"): confirmed}):
			booking = self.settings.create_shipment(self.shipment())

		self.assertEqual(self.calls[0][2]["external_id"], "SHIP-26-00001")
		self.assertEqual(self.calls[0][2]["shipping"], "STANDARD")
		self.assertEqual(booking["order_ref"], "1001")
		self.assertEqual(booking["tracking_ref"], "1001")
		self.assertIsNone(booking["awb"])
		self.assertEqual(booking["cost_amount"], 4.99)

	def test_test_mode_leaves_the_order_a_draft(self):
		self.settings.test_mode = 1
		with self.stub_printful({("POST", "/orders"): {"id": 1001, "status": "draft"}}):
			booking = self.settings.create_shipment(self.shipment())

		self.assertEqual([call[1] for call in self.calls], ["/orders"])
		self.assertEqual(booking["status"], "draft")

	def test_failed_confirm_keeps_the_draft_for_a_resume(self):
		responses = {
			("POST", "/orders"): {"id": 1001, "status": "draft"},
			("POST", "/orders/1001/confirm"): frappe.ValidationError("card declined"),
		}
		with self.stub_printful(responses), self.assertRaises(PartialBookingError) as raised:
			self.settings.create_shipment(self.shipment())

		self.assertEqual(raised.exception.booking, {"order_ref": "1001", "shipment_ref": "1001"})

	def test_a_shipment_without_items_is_refused(self):
		shipment = {**self.shipment(), "parcels": [{"weight": 1}]}
		with self.stub_printful({}):
			self.assertRaises(frappe.ValidationError, self.settings.create_shipment, shipment)

	def test_capabilities(self):
		self.assertTrue(PrintfulShippingSettings.supports("resume"))
		self.assertFalse(PrintfulShippingSettings.supports("pickup"))
		self.assertFalse(PrintfulShippingSettings.supports("manifest"))


class TestPrintfulWebhook(PrintfulTestCase):
	def deliver(self, event: dict) -> dict:
		return self.settings.handle_webhook(json.dumps(event).encode(), {})

	def test_status_is_read_back_from_printful_not_the_payload(self):
		# The payload claims the order was canceled; Printful says it shipped, and Printful wins.
		event = {
			"type": "order_canceled",
			"created": 1,
			"data": {"order": {"id": 1001, "status": "canceled"}},
		}
		with self.stub_printful({("GET", "/orders/1001"): SHIPPED_ORDER}):
			result = self.deliver(event)

		self.assertEqual(result["status"], "In Transit")
		self.assertEqual(result["tracking_ref"], "1001")
		self.assertEqual(result["awb"], "9400100000000000000000")
		self.assertEqual(result["event_id"], "order_canceled:1")

	def test_an_order_id_that_is_not_a_number_is_ignored(self):
		with self.stub_printful({}):
			self.assertEqual(self.deliver({"data": {"order": {"id": "../stores"}}}), {})
		self.assertEqual(self.calls, [])

	def test_an_order_needing_a_human_is_ignored(self):
		with self.stub_printful({("GET", "/orders/1001"): {"id": 1001, "status": "failed"}}):
			self.assertEqual(self.deliver({"data": {"order": {"id": 1001}}}), {})


class TestPartnerHandover(IntegrationTestCase):
	def test_handover_fills_only_blanks(self):
		request = frappe.new_doc("Shipping Request")
		request.carrier = "Booked Carrier"
		recorded = request.record_handover({"awb": "AWB1", "carrier": "USPS", "tracking_url": None})

		self.assertTrue(recorded)
		self.assertEqual(request.awb, "AWB1")
		self.assertEqual(request.carrier, "Booked Carrier")

	def test_nothing_reported_records_nothing(self):
		self.assertFalse(frappe.new_doc("Shipping Request").record_handover(None))

	def test_delivery_note_lines_reach_the_provider(self):
		"""Shipping Parcel rows drop line detail, so the payload reads it back from the Delivery Note."""
		request = frappe.new_doc("Shipping Request")
		request.delivery_note = "_Test DN"
		request.append("parcels", {"length": 30, "width": 20, "height": 10, "weight": 0.4, "count": 1})
		delivery_note = SimpleNamespace(
			items=[SimpleNamespace(item_name="Tee", item_code="PF-TEE", qty=2, rate=25)]
		)

		with patch.object(frappe, "get_cached_doc", return_value=delivery_note):
			parcels = request.get_parcels_with_items()

		self.assertEqual(
			parcels[0]["items"], [{"description": "Tee", "sku": "PF-TEE", "quantity": 2, "rate": 25.0}]
		)


class TestStateCode(IntegrationTestCase):
	def test_a_state_name_becomes_its_code(self):
		self.assertEqual(state_code("US", "California"), "CA")

	def test_a_code_passes_through(self):
		self.assertEqual(state_code("US", "ca"), "CA")

	def test_no_state_is_none(self):
		self.assertIsNone(state_code("GB", ""))
