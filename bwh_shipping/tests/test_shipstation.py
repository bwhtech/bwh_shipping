# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

import base64
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import frappe
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from frappe.tests import IntegrationTestCase
from jwt.algorithms import RSAAlgorithm

from bwh_shipping.bwh_shipping.doctype.shipstation_shipping_settings.shipstation_shipping_settings import (
	SIGNING_KEYS_CACHE_KEY,
	ShipStationShippingSettings,
	build_carrier_choice,
	state_province,
)

SETTINGS_DOCTYPE = "ShipStation Shipping Settings"

ORIGIN = {
	"contact_name": "Warehouse",
	"line1": "4301 Bull Creek Rd",
	"city": "Austin",
	"state": "Texas",
	"pincode": "78731",
	"country": "United States",
	"phone": "5555555555",
}

DESTINATION = {
	"contact_name": "Jane Doe",
	"line1": "525 S Winchester Blvd",
	"city": "San Jose",
	"state": "CA",
	"pincode": "95128",
	"country": "United States",
}

PARCELS = [{"length": 30, "width": 20, "height": 10, "weight": 1.5, "count": 2}]

RATE = {
	"carrier_id": "se-123",
	"service_code": "ups_ground",
	"service_type": "UPS Ground",
	"carrier_code": "ups",
	"shipping_amount": {"currency": "usd", "amount": 10.1},
	"insurance_amount": {"currency": "usd", "amount": 0.0},
	"confirmation_amount": {"currency": "usd", "amount": 0.0},
	"other_amount": {"currency": "usd", "amount": 1.52},
	"delivery_days": 3,
	"error_messages": [],
}

LABEL = {
	"label_id": "se-label-1",
	"shipment_id": "se-shipment-1",
	"status": "completed",
	"tracking_number": "1Z999",
	"carrier_code": "ups",
	"shipment_cost": {"currency": "usd", "amount": 17.58},
	"label_download": {"href": "https://api.shipstation.com/v2/downloads/label-1.pdf"},
}

TRACK_EVENT = {
	"resource_url": "https://api.shipstation.com/v2/labels/se-label-1/track",
	"resource_type": "TRACK_EVENT_V2",
	"data": {
		"tracking_number": "1Z999",
		"status_code": "IT",
		"status_description": "In Transit",
		"events": [
			{
				"occurred_at": "2026-10-28T12:32:00Z",
				"description": "Arrived at UPS Facility",
				"city_locality": "OCEANSIDE",
				"state_province": "CA",
				"status_code": "IT",
			}
		],
	},
}


class ShipStationTestCase(IntegrationTestCase):
	def setUp(self):
		self.settings = frappe.get_single(SETTINGS_DOCTYPE)
		self.settings.carrier_id = None
		self.settings.currency = "USD"
		self.calls = []

	def stub_shipstation(self, responses: dict):
		"""Neutralise only the outbound boundary: each (method, path) answers from `responses`."""

		def request(settings, method, path, payload=None, timeout=None):
			self.calls.append((method, path, payload))
			return responses[(method, path)]

		return patch.object(ShipStationShippingSettings, "request", request)


class TestShipStationRates(ShipStationTestCase):
	def test_every_connected_carrier_is_rated_in_one_call(self):
		responses = {
			("GET", "/carriers"): {"carriers": [{"carrier_id": "se-123"}, {"carrier_id": "se-456"}]},
			("POST", "/rates"): {"rate_response": {"rates": [RATE]}},
		}
		with self.stub_shipstation(responses):
			quoted = self.settings.get_rates(ORIGIN, DESTINATION, PARCELS)

		payload = self.calls[1][2]
		# Only the package type a label is booked as, so a flat-rate box price is never quoted.
		self.assertEqual(
			payload["rate_options"], {"carrier_ids": ["se-123", "se-456"], "package_types": ["package"]}
		)
		self.assertEqual(len(payload["shipment"]["packages"]), 2)
		self.assertEqual(payload["shipment"]["packages"][0]["weight"], {"value": 1.5, "unit": "kilogram"})
		self.assertEqual(payload["shipment"]["ship_from"]["state_province"], "TX")
		self.assertEqual(payload["shipment"]["ship_to"]["country_code"], "US")
		self.assertEqual(
			quoted,
			[
				{
					"service_code": "se-123|ups_ground",
					"service_name": "UPS Ground",
					"carrier": "ups",
					"amount": 11.62,
					"currency": "USD",
					"transit_days": 3,
					"cod_available": False,
				}
			],
		)

	def test_the_configured_carrier_is_rated_without_listing_carriers(self):
		self.settings.carrier_id = "se-123"
		with self.stub_shipstation({("POST", "/rates"): {"rate_response": {"rates": [RATE]}}}):
			self.settings.get_rates(ORIGIN, DESTINATION, PARCELS)

		self.assertEqual([call[1] for call in self.calls], ["/rates"])

	def test_a_rate_with_errors_is_not_quoted(self):
		failed = {**RATE, "error_messages": ["Service not available to this destination"]}
		self.settings.carrier_id = "se-123"
		with self.stub_shipstation({("POST", "/rates"): {"rate_response": {"rates": [failed]}}}):
			self.assertEqual(self.settings.get_rates(ORIGIN, DESTINATION, PARCELS), [])


class TestShipStationBooking(ShipStationTestCase):
	def shipment(self, service_code="se-123|ups_ground"):
		return {
			"reference": "SHIP-26-00001",
			"service_code": service_code,
			"origin": ORIGIN,
			"destination": DESTINATION,
			"parcels": PARCELS,
		}

	def test_a_label_is_bought_in_one_call(self):
		with self.stub_shipstation({("POST", "/labels"): LABEL}):
			booking = self.settings.create_shipment(self.shipment())

		shipment = self.calls[0][2]["shipment"]
		self.assertEqual((shipment["carrier_id"], shipment["service_code"]), ("se-123", "ups_ground"))
		self.assertEqual(shipment["external_shipment_id"], "SHIP-26-00001")
		self.assertEqual(booking["order_ref"], "se-shipment-1")
		self.assertEqual(booking["shipment_ref"], "se-label-1")
		self.assertEqual(booking["awb"], "1Z999")
		self.assertEqual(booking["cost_amount"], 17.58)
		self.assertEqual(booking["cost_currency"], "USD")

	def test_a_service_code_without_a_carrier_is_refused(self):
		with self.stub_shipstation({}):
			self.assertRaises(
				frappe.ValidationError, self.settings.create_shipment, self.shipment("ups_ground")
			)
		self.assertEqual(self.calls, [])

	def test_capabilities(self):
		self.assertTrue(ShipStationShippingSettings.supports("service_choices"))
		self.assertFalse(ShipStationShippingSettings.supports("resume"))
		self.assertFalse(ShipStationShippingSettings.supports("pickup"))
		self.assertFalse(ShipStationShippingSettings.supports("manifest"))

	def test_test_mode_refuses_a_production_key(self):
		self.settings.test_mode = 1
		self.settings.api_key = "live-key"
		self.assertRaises(frappe.ValidationError, self.settings.validate)

		self.settings.api_key = "TEST_sandbox-key"
		self.settings.validate()


class TestShipStationCancel(ShipStationTestCase):
	def test_an_approved_void_cancels(self):
		void = {"approved": True, "message": "This label has been voided."}
		with self.stub_shipstation({("PUT", "/labels/se-label-1/void"): void}):
			result = self.settings.cancel_shipment("se-shipment-1", shipment_ref="se-label-1")

		self.assertEqual(result["status"], "voided")

	def test_a_refused_void_raises(self):
		void = {"approved": False, "message": "The label has been scanned by the carrier."}
		with self.stub_shipstation({("PUT", "/labels/se-label-1/void"): void}):
			self.assertRaises(
				frappe.ValidationError,
				self.settings.cancel_shipment,
				"se-shipment-1",
				shipment_ref="se-label-1",
			)


class TestShipStationTracking(ShipStationTestCase):
	def track(self, data: dict) -> dict:
		with self.stub_shipstation({("GET", "/labels/se-label-1/track"): data}):
			return self.settings.get_tracking("1Z999", shipment_ref="se-label-1")

	def test_the_detail_code_wins_over_the_status_code(self):
		tracking = self.track({"status_code": "IT", "status_detail_code": "OUT_FOR_DELIVERY"})
		self.assertEqual(tracking["status"], "Out For Delivery")

	def test_an_exception_is_left_for_a_human(self):
		self.assertIsNone(self.track({"status_code": "EX"})["status"])

	def test_events_are_read(self):
		event = self.track(TRACK_EVENT["data"])["events"][0]
		self.assertEqual(event["location"], "OCEANSIDE, CA")
		self.assertEqual(event["message"], "Arrived at UPS Facility")


class TestShipStationWebhook(ShipStationTestCase):
	def setUp(self):
		super().setUp()
		frappe.cache.delete_value(SIGNING_KEYS_CACHE_KEY)
		self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
		self.jwk = {**RSAAlgorithm.to_jwk(self.private_key.public_key(), as_dict=True), "kid": "key-1"}

	def headers(self, body: bytes, timestamp: str | None = None, key_id: str = "key-1") -> dict:
		timestamp = timestamp or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
		signature = self.private_key.sign(
			f"{timestamp}.".encode() + body, padding.PKCS1v15(), hashes.SHA256()
		)
		return {
			# Werkzeug's title case, as webhook.handle hands it over.
			"X-Shipengine-Rsa-Sha256-Key-Id": key_id,
			"X-Shipengine-Rsa-Sha256-Signature": base64.b64encode(signature).decode(),
			"X-Shipengine-Timestamp": timestamp,
		}

	def deliver(self, body: bytes, headers: dict) -> dict:
		with patch.object(ShipStationShippingSettings, "fetch_signing_keys", return_value=[self.jwk]):
			return self.settings.handle_webhook(body, headers)

	def test_a_signed_track_event_is_applied(self):
		body = json.dumps(TRACK_EVENT).encode()
		result = self.deliver(body, self.headers(body))

		self.assertEqual(result["awb"], "1Z999")
		self.assertEqual(result["status"], "In Transit")
		self.assertEqual(result["event_id"], "1Z999:IT:2026-10-28T12:32:00Z")

	def test_a_replay_has_the_same_event_id(self):
		body = json.dumps(TRACK_EVENT).encode()
		first = self.deliver(body, self.headers(body))
		second = self.deliver(body, self.headers(body))
		self.assertEqual(first["event_id"], second["event_id"])

	def test_a_tampered_body_is_refused(self):
		body = json.dumps(TRACK_EVENT).encode()
		headers = self.headers(body)
		forged = body.replace(b'"IT"', b'"DE"')
		self.assertRaises(frappe.ValidationError, self.deliver, forged, headers)

	def test_an_old_delivery_is_refused(self):
		body = json.dumps(TRACK_EVENT).encode()
		stale = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
		self.assertRaises(frappe.ValidationError, self.deliver, body, self.headers(body, timestamp=stale))

	def test_an_unknown_key_is_refused(self):
		body = json.dumps(TRACK_EVENT).encode()
		self.assertRaises(frappe.ValidationError, self.deliver, body, self.headers(body, key_id="other"))

	def test_an_unsigned_delivery_is_refused(self):
		self.assertRaises(frappe.ValidationError, self.deliver, json.dumps(TRACK_EVENT).encode(), {})

	def test_other_events_are_ignored(self):
		body = json.dumps({"resource_type": "BATCH_PROCESSED_V2"}).encode()
		self.assertEqual(self.deliver(body, self.headers(body)), {})


class TestShipStationHelpers(IntegrationTestCase):
	def test_carrier_services_become_service_codes(self):
		carrier = {
			"carrier_id": "se-123",
			"carrier_code": "ups",
			"nickname": "My UPS",
			"services": [{"service_code": "ups_ground", "name": "UPS Ground"}],
		}
		self.assertEqual(
			build_carrier_choice(carrier),
			{
				"carrier": "ups",
				"description": "My UPS",
				"services": [{"service_code": "se-123|ups_ground", "service_name": "UPS Ground"}],
			},
		)

	def test_a_state_name_becomes_its_code(self):
		self.assertEqual(state_province("US", "California"), "CA")
		self.assertIsNone(state_province("GB", ""))
