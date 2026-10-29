# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

import base64
from datetime import UTC, datetime

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import get_request_session
from frappe.utils.data import cint, cstr, flt
from jwt.algorithms import RSAAlgorithm

from bwh_shipping.base_class import ShippingProviderBase
from bwh_shipping.bwh_shipping.doctype.aftership_shipping_settings.aftership_shipping_settings import (
	build_service_code,
	split_service_code,
)
from bwh_shipping.bwh_shipping.utils import get_provider_profile
from bwh_shipping.units import to_system_datetime

SHIPSTATION_BASE_URL = "https://api.shipstation.com/v2"
SHIPSTATION_JWKS_URL = "https://api.shipengine.com/jwks"
DEFAULT_TIMEOUT_SECONDS = 30
SANDBOX_KEY_PREFIX = "TEST_"
BOOKED_PACKAGE_TYPE = "package"

# ShipStation's tracking status codes, mapped onto the canonical ladder. `EX` (exception) and `UN`
# (unknown) are deliberately absent: each needs a human, and mapping them would let a webhook move a shipment.
SHIPSTATION_STATUS_MAP = {
	"NY": "Ready To Ship",
	"AC": "In Transit",
	"IT": "In Transit",
	"SP": "Out For Delivery",
	"AT": "Undelivered",
	"DE": "Delivered",
}

# The detail code is finer than the status code, and the only place out-for-delivery and returns show up.
SHIPSTATION_DETAIL_MAP = {
	"OUT_FOR_DELIVERY": "Out For Delivery",
	"RETURN_TO_SENDER": "RTO",
}

SIGNATURE_HEADERS = (
	"x-shipengine-rsa-sha256-key-id",
	"x-shipengine-rsa-sha256-signature",
	"x-shipengine-timestamp",
)
WEBHOOK_TOLERANCE_SECONDS = 5 * 60
SIGNING_KEYS_CACHE_KEY = "shipstation_webhook_signing_keys"
SIGNING_KEYS_CACHE_SECONDS = 24 * 60 * 60
RSA_SHA256 = RSAAlgorithm(RSAAlgorithm.SHA256)

RATE_AMOUNT_FIELDS = ("shipping_amount", "insurance_amount", "confirmation_amount", "other_amount")


class ShipStationShippingSettings(Document, ShippingProviderBase):
	def get_provider_name(self) -> str:
		return "ShipStation"

	def validate(self):
		# The key alone picks the sandbox, so a production key would buy real labels with Test Mode on.
		api_key = self.get_password("api_key", raise_exception=False)
		if self.test_mode and api_key and not api_key.startswith(SANDBOX_KEY_PREFIX):
			frappe.throw(
				_("Test Mode needs a ShipStation sandbox key, which starts with {0}.").format(
					frappe.bold(SANDBOX_KEY_PREFIX)
				)
			)

	# --- HTTP ----------------------------------------------------------------

	def get_headers(self) -> dict:
		api_key = self.get_password("api_key", raise_exception=False)
		if not api_key:
			frappe.throw(_("Set the ShipStation API Key before using this provider"))
		return {"API-Key": api_key, "Content-Type": "application/json"}

	def request(self, method: str, path: str, payload: dict | None = None, timeout: int | None = None):
		"""Call ShipStation. A failed call answers with an `errors` list, a successful one with the resource."""
		try:
			response = get_request_session().request(
				method,
				f"{SHIPSTATION_BASE_URL}{path}",
				headers=self.get_headers(),
				json=payload,
				timeout=timeout or DEFAULT_TIMEOUT_SECONDS,
			)
		except Exception as exception:
			self.log_request(path, error=exception)
			frappe.throw(_("Could not reach ShipStation: {0}").format(cstr(exception)))

		body = self.read_body(response, path)
		if not response.ok:
			message = read_shipstation_error(body) or f"HTTP {response.status_code}"
			self.log_request(path, error=message)
			frappe.throw(_("ShipStation rejected the request: {0}").format(message))

		self.log_request(path, output=summarise(body))
		return body

	def read_body(self, response, path: str) -> dict:
		try:
			body = response.json()
		except ValueError:
			self.log_request(path, error="ShipStation returned a non-JSON body")
			frappe.throw(_("ShipStation returned an unreadable response"))
		return body if isinstance(body, dict) else {}

	def log_request(self, path: str, output=None, error=None):
		# The payload carries the customer's name, address and phone number, so only the endpoint and the
		# outcome are recorded.
		create_request_log(
			{"endpoint": path},
			service_name="ShipStation",
			is_remote_request=True,
			reference_doctype=self.doctype,
			reference_docname=self.name,
			output=output,
			error=error,
			status="Failed" if error else "Completed",
		)

	# --- Contract ------------------------------------------------------------

	def get_rates(
		self,
		origin: dict,
		destination: dict,
		parcels: list[dict],
		cod: bool = False,
		declared_value: float = 0.0,
	) -> list[dict]:
		"""One call rates every carrier named in `carrier_ids`."""
		carrier_ids = self.get_carrier_ids()
		if not carrier_ids:
			return []

		payload = {
			# A label is booked as a plain package, so a flat-rate box price would quote less than is charged.
			"rate_options": {"carrier_ids": carrier_ids, "package_types": [BOOKED_PACKAGE_TYPE]},
			"shipment": build_shipment(origin, destination, parcels),
		}
		body = self.request("POST", "/rates", payload, timeout=cint(self.rates_timeout_seconds) or 10)
		rates = (body.get("rate_response") or {}).get("rates") or []
		return [self.build_rate(rate) for rate in rates if is_quotable(rate)]

	def get_carrier_ids(self) -> list[str]:
		if self.carrier_id:
			return [self.carrier_id]
		return [carrier["carrier_id"] for carrier in self.list_carriers() if carrier.get("carrier_id")]

	def list_carriers(self) -> list[dict]:
		return self.request("GET", "/carriers").get("carriers") or []

	def build_rate(self, rate: dict) -> dict:
		return {
			# Buying a label needs the carrier account as well as the service, so both travel in one code.
			"service_code": build_service_code(rate.get("carrier_id"), rate.get("service_code")),
			"service_name": rate.get("service_type") or rate.get("service_code"),
			"carrier": rate.get("carrier_code"),
			"amount": total_amount(rate),
			"currency": read_currency(rate.get("shipping_amount")) or self.currency,
			"transit_days": cint(rate.get("delivery_days")),
			"cod_available": False,
		}

	def create_shipment(self, shipment: dict) -> dict:
		"""ShipStation buys a label in one call, so there is no partial state to recover from."""
		carrier_id, service_code = split_service_code(shipment.get("service_code"))
		if not (carrier_id and service_code):
			frappe.throw(_("A ShipStation service code must name both a carrier ID and a service code"))

		payload = {
			"label_format": "pdf",
			"shipment": {
				"carrier_id": carrier_id,
				"service_code": service_code,
				# Finds the label in ShipStation's dashboard from the Shipping Request.
				"external_shipment_id": cstr(shipment.get("reference")),
				**build_shipment(
					shipment.get("origin"), shipment.get("destination"), shipment.get("parcels")
				),
			},
		}
		label = self.request("POST", "/labels", payload)
		cost = label.get("shipment_cost") or {}
		return {
			"order_ref": label.get("shipment_id"),
			"shipment_ref": label.get("label_id"),
			"awb": label.get("tracking_number"),
			"carrier": label.get("carrier_code"),
			"label_url": (label.get("label_download") or {}).get("href"),
			"cost_amount": flt(cost.get("amount")),
			"cost_currency": read_currency(cost) or self.currency,
			"status": label.get("status"),
		}

	def cancel_shipment(
		self, order_ref: str, shipment_ref: str | None = None, awb: str | None = None
	) -> dict:
		"""Void the label. A void is final, and the carrier can refuse it once the parcel is scanned."""
		result = self.request("PUT", f"/labels/{shipment_ref}/void")
		if not result.get("approved"):
			frappe.throw(_("ShipStation did not void the label: {0}").format(cstr(result.get("message"))))
		return {"status": "voided", "message": result.get("message")}

	def get_tracking(
		self, awb: str, shipment_ref: str | None = None, tracking_ref: str | None = None
	) -> dict:
		if not shipment_ref:
			return {"status": None, "provider_status": None, "events": []}
		return build_tracking(self.request("GET", f"/labels/{shipment_ref}/track"))

	def handle_webhook(self, payload: bytes, headers: dict) -> dict:
		"""ShipStation signs its webhooks: RSA-SHA256 over "<timestamp>.<raw body>", with a published key."""
		self.verify_signature(payload, headers)

		event = frappe.parse_json(payload.decode())
		if event.get("resource_type") != "TRACK_EVENT_V2":
			return {}

		data = event.get("data") or {}
		tracking = build_tracking(data)
		awb = cstr(data.get("tracking_number"))
		if not (awb and tracking["status"]):
			return {}
		return {
			**tracking,
			"awb": awb,
			"event_id": f"{awb}:{data.get('status_code')}:{latest_event_time(data.get('events'))}",
		}

	# Pickups and manifests exist in ShipStation's API, but each needs carrier-specific windows and daily
	# cut-offs; schedule_pickup and generate_manifest stay unimplemented until a store asks for them.

	# --- Helpers -------------------------------------------------------------

	def verify_signature(self, payload: bytes, headers: dict):
		headers = {key.casefold(): value for key, value in headers.items()}
		key_id, signature, timestamp = (headers.get(name) for name in SIGNATURE_HEADERS)
		if not (key_id and signature and timestamp):
			frappe.throw(_("ShipStation webhook signature headers are missing"))
		if not is_recent(timestamp):
			frappe.throw(_("ShipStation webhook timestamp is too old to trust"))

		# Signed over the raw bytes, so a re-serialised body fails every delivery.
		signed = f"{timestamp}.".encode() + payload
		if not RSA_SHA256.verify(signed, self.get_signing_key(key_id), base64.b64decode(signature)):
			frappe.throw(_("ShipStation webhook signature does not match"))

	def get_signing_key(self, key_id: str):
		"""The key set changes rarely, so it is cached; an unknown key id fetches it again once."""
		jwk = find_signing_key(frappe.cache.get_value(SIGNING_KEYS_CACHE_KEY), key_id)
		if not jwk:
			keys = self.fetch_signing_keys()
			frappe.cache.set_value(SIGNING_KEYS_CACHE_KEY, keys, expires_in_sec=SIGNING_KEYS_CACHE_SECONDS)
			jwk = find_signing_key(keys, key_id)
		if not jwk:
			frappe.throw(_("ShipStation signed this webhook with an unknown key"))
		return RSAAlgorithm.from_jwk(jwk)

	def fetch_signing_keys(self) -> list[dict]:
		# Public keys, so the API key is not sent.
		try:
			response = get_request_session().get(SHIPSTATION_JWKS_URL, timeout=DEFAULT_TIMEOUT_SECONDS)
			response.raise_for_status()
			return response.json().get("keys") or []
		except Exception as exception:
			frappe.throw(_("Could not read ShipStation's webhook signing keys: {0}").format(cstr(exception)))

	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Prove the key works and name the carriers it can buy labels from."""
		carriers = self.list_carriers()
		if not carriers:
			frappe.throw(_("This ShipStation account has no connected carriers, so no label can be bought."))
		return {
			"carriers": [
				{
					"carrier_id": carrier.get("carrier_id"),
					"carrier_code": carrier.get("carrier_code"),
					"nickname": carrier.get("nickname"),
				}
				for carrier in carriers
			]
		}

	@frappe.whitelist()
	def get_service_choices(self) -> dict:
		"""Every service on the connected carriers, so an admin picks one instead of typing a Service Code."""
		carriers = self.list_carriers()
		if self.carrier_id:
			# Rates are only shopped against this carrier, so a service of any other could never be quoted.
			carriers = [carrier for carrier in carriers if carrier.get("carrier_id") == self.carrier_id]
		return {
			"provider": get_provider_profile(self.doctype),
			"accounts": [choice for choice in map(build_carrier_choice, carriers) if choice["services"]],
		}


def build_shipment(origin: dict | None, destination: dict | None, parcels: list[dict] | None) -> dict:
	return {
		"ship_from": build_address(origin or {}),
		"ship_to": build_address(destination or {}),
		"packages": [
			build_package(parcel)
			for parcel in parcels or []
			for _count in range(cint(parcel.get("count")) or 1)
		],
	}


def build_package(parcel: dict) -> dict:
	"""ShipStation takes kilograms and centimetres, the units at the contract boundary."""
	return {
		"weight": {"value": flt(parcel.get("weight")), "unit": "kilogram"},
		"dimensions": {
			"length": flt(parcel.get("length")),
			"width": flt(parcel.get("width")),
			"height": flt(parcel.get("height")),
			"unit": "centimeter",
		},
	}


def build_address(address: dict) -> dict:
	country = address.get("country")
	country_code = cstr(frappe.db.get_value("Country", country, "code")).upper() if country else ""
	payload = {
		"name": address.get("contact_name") or address.get("company_name"),
		"company_name": address.get("company_name"),
		"email": address.get("email"),
		"address_line1": address.get("line1"),
		"address_line2": address.get("line2"),
		"city_locality": address.get("city"),
		"state_province": state_province(country_code, address.get("state")),
		"postal_code": address.get("pincode"),
		"country_code": country_code,
		# Lets the carrier decide, rather than this app guessing at a residential surcharge.
		"address_residential_indicator": "unknown",
	}
	# ShipStation requires the phone key even where the carrier needs no number.
	return {**{key: value for key, value in payload.items() if value}, "phone": cstr(address.get("phone"))}


def state_province(country_code: str, state: str | None) -> str | None:
	"""Carriers want the ISO 3166-2 code ("CA"), and an ERPNext Address holds whatever was typed."""
	state = cstr(state).strip()
	if len(state) <= 3:
		return state.upper() or None
	try:
		import pycountry

		for subdivision in pycountry.subdivisions.get(country_code=country_code) or []:
			if subdivision.name.casefold() == state.casefold():
				return subdivision.code.split("-", 1)[1]
	except ImportError:
		pass
	return state


def is_quotable(rate: dict) -> bool:
	# A carrier that cannot serve the route answers with error messages and no price.
	if rate.get("error_messages"):
		return False
	return bool(rate.get("carrier_id") and rate.get("service_code")) and total_amount(rate) > 0


def total_amount(rate: dict) -> float:
	"""ShipStation itemises a rate, and the shopper pays the sum."""
	return flt(sum(flt((rate.get(field) or {}).get("amount")) for field in RATE_AMOUNT_FIELDS), 2)


def read_currency(money: dict | None) -> str | None:
	# ShipStation spells currencies in lowercase ("usd"); Currency names are uppercase.
	return cstr((money or {}).get("currency")).upper() or None


def build_tracking(data: dict) -> dict:
	return {
		"status": SHIPSTATION_DETAIL_MAP.get(data.get("status_detail_code"))
		or SHIPSTATION_STATUS_MAP.get(data.get("status_code")),
		"provider_status": data.get("status_detail_description") or data.get("status_description"),
		"events": [build_event(event) for event in data.get("events") or []],
	}


def build_event(event: dict) -> dict:
	location = ", ".join(
		cstr(part)
		for part in (event.get("city_locality"), event.get("state_province"), event.get("country_code"))
		if part
	)
	return {
		"timestamp": to_system_datetime(event.get("occurred_at")),
		"status": event.get("status_code") or event.get("event_code"),
		"location": location or None,
		"message": event.get("description"),
	}


def latest_event_time(events: list | None) -> str:
	return max((cstr(event.get("occurred_at")) for event in events or []), default="")


def build_carrier_choice(carrier: dict) -> dict:
	return {
		"carrier": carrier.get("carrier_code"),
		"description": carrier.get("nickname") or carrier.get("friendly_name") or carrier.get("carrier_code"),
		"services": [
			{
				"service_code": build_service_code(carrier.get("carrier_id"), service.get("service_code")),
				"service_name": service.get("name") or service.get("service_code"),
			}
			for service in carrier.get("services") or []
			if service.get("service_code")
		],
	}


def is_recent(timestamp: str) -> bool:
	"""Refuses a replayed delivery. Clocks drift, so a timestamp slightly in the future is accepted too."""
	try:
		sent = datetime.fromisoformat(timestamp)
	except ValueError:
		return False
	if sent.tzinfo is None:
		sent = sent.replace(tzinfo=UTC)
	return abs((datetime.now(UTC) - sent).total_seconds()) <= WEBHOOK_TOLERANCE_SECONDS


def find_signing_key(keys: list | None, key_id: str) -> dict | None:
	return next((key for key in keys or [] if key.get("kid") == key_id and key.get("kty") == "RSA"), None)


def read_shipstation_error(body: dict) -> str | None:
	messages = [cstr(error.get("message")) for error in body.get("errors") or [] if error.get("message")]
	return "; ".join(messages) or None


def summarise(body: dict) -> dict:
	"""ShipStation echoes the customer's name, address and phone number, so only ids are logged."""
	keys = ("request_id", "shipment_id", "label_id", "status", "tracking_number", "approved")
	return {key: body[key] for key in keys if key in body}
