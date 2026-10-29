# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import get_request_session
from frappe.utils.data import cint, cstr, flt

from bwh_shipping.base_class import ShippingProviderBase
from bwh_shipping.exceptions import PartialBookingError
from bwh_shipping.units import to_system_datetime

PRINTFUL_BASE_URL = "https://api.printful.com"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_CURRENCY = "USD"

# Printful's order statuses, mapped onto the canonical ladder. `fulfilled` means every package has left
# Printful, not that it arrived: Printful never reports delivery, so the shopper follows the tracking link.
# `draft`, `failed` and `onhold` are deliberately absent: each needs a human in the Printful dashboard.
PRINTFUL_STATUS_MAP = {
	"pending": "Ready To Ship",
	"inreview": "Ready To Ship",
	"inprocess": "Ready To Ship",
	"partial": "In Transit",
	"fulfilled": "In Transit",
	"canceled": "Cancelled",
}

RATE_RECIPIENT_FIELDS = ("address1", "address2", "city", "state_code", "country_code", "zip", "phone")


class PrintfulShippingSettings(Document, ShippingProviderBase):
	def get_provider_name(self) -> str:
		return "Printful"

	# --- HTTP ----------------------------------------------------------------

	def get_headers(self) -> dict:
		api_token = self.get_password("api_token", raise_exception=False)
		if not api_token:
			frappe.throw(_("Set the Printful API Token before using this provider"))
		headers = {"Authorization": f"Bearer {api_token}"}
		if self.store_id:
			headers["X-PF-Store-Id"] = cstr(self.store_id)
		return headers

	def request(self, method: str, path: str, payload: dict | None = None, timeout: int | None = None):
		"""Call Printful and unwrap its {"code", "result"} envelope."""
		try:
			response = get_request_session().request(
				method,
				f"{PRINTFUL_BASE_URL}{path}",
				headers=self.get_headers(),
				json=payload,
				timeout=timeout or DEFAULT_TIMEOUT_SECONDS,
			)
		except Exception as exception:
			self.log_request(path, error=exception)
			frappe.throw(_("Could not reach Printful: {0}").format(cstr(exception)))

		body = self.read_body(response, path)
		if not response.ok:
			message = read_printful_error(body) or f"HTTP {response.status_code}"
			self.log_request(path, error=message)
			frappe.throw(_("Printful rejected the request: {0}").format(message))

		self.log_request(path, output=summarise(body))
		return body.get("result")

	def read_body(self, response, path: str) -> dict:
		try:
			body = response.json()
		except ValueError:
			self.log_request(path, error="Printful returned a non-JSON body")
			frappe.throw(_("Printful returned an unreadable response"))
		return body if isinstance(body, dict) else {}

	def log_request(self, path: str, output=None, error=None):
		# The payload carries the customer's name, address and phone number, so only the endpoint and the
		# outcome are recorded.
		create_request_log(
			{"endpoint": path},
			service_name="Printful",
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
		"""Printful rates the products themselves, from its own facilities, so `origin` is not sent.

		A cart that lists no items cannot be rated at all; that is the normal answer, not an error.
		"""
		items = get_items(parcels)
		if not (items and destination):
			return []

		recipient = self.build_recipient(destination)
		payload = {
			"recipient": {key: recipient[key] for key in RATE_RECIPIENT_FIELDS if key in recipient},
			"items": [build_rate_item(item) for item in items],
			"currency": self.get_currency(),
		}
		rates = self.request(
			"POST", "/shipping/rates", payload, timeout=cint(self.rates_timeout_seconds) or 10
		)
		return [self.build_rate(rate) for rate in rates or [] if rate.get("id") and flt(rate.get("rate")) > 0]

	def build_rate(self, rate: dict) -> dict:
		return {
			"service_code": cstr(rate.get("id")),
			"service_name": rate.get("name") or rate.get("id"),
			"carrier": "Printful",
			"amount": flt(rate.get("rate")),
			"currency": rate.get("currency") or self.get_currency(),
			"transit_days": cint(rate.get("maxDeliveryDays")),
			"cod_available": False,
		}

	def create_shipment(self, shipment: dict) -> dict:
		"""Printful books in two calls: create a draft order, then confirm it for fulfilment.

		The draft costs nothing; confirming is what Printful charges for. A failed confirm carries the
		draft's id back out, so the retry confirms that same order rather than creating a second one.
		"""
		order = self.request("POST", "/orders", self.build_order(shipment)) or {}
		order_ref = cstr(order.get("id"))
		if not order_ref:
			frappe.throw(_("Printful created no order for this shipment"))
		if self.test_mode:
			return self.build_booking(order)

		try:
			return self.resume_booking(order_ref, order_ref)
		except Exception as exception:
			booking = {"order_ref": order_ref, "shipment_ref": order_ref}
			raise PartialBookingError(cstr(exception), booking=booking) from exception

	def resume_booking(self, order_ref: str, shipment_ref: str, service_code: str | None = None) -> dict:
		"""Confirm a draft that already exists at Printful, without creating a second order."""
		return self.build_booking(self.request("POST", f"/orders/{order_ref}/confirm") or {})

	def build_booking(self, order: dict) -> dict:
		order_ref = cstr(order.get("id"))
		costs = order.get("costs") or {}
		return {
			"order_ref": order_ref,
			"shipment_ref": order_ref,
			# Printful picks the carrier and buys the label when it ships; tracking fills these in then.
			"awb": None,
			"carrier": None,
			"label_url": None,
			"cost_amount": flt(costs.get("shipping")),
			"cost_currency": costs.get("currency") or self.get_currency(),
			"status": order.get("status"),
			"tracking_ref": order_ref,
		}

	def cancel_shipment(
		self, order_ref: str, shipment_ref: str | None = None, awb: str | None = None
	) -> dict:
		order = self.request("DELETE", f"/orders/{order_ref}") or {}
		return {"status": order.get("status") or "canceled", "message": None}

	def get_tracking(
		self, awb: str, shipment_ref: str | None = None, tracking_ref: str | None = None
	) -> dict:
		return self.read_order(tracking_ref or shipment_ref)

	def handle_webhook(self, payload: bytes, headers: dict) -> dict:
		"""Printful does not sign its webhooks, so the payload is only a hint.

		The order it names is read back with the store's own token, and that answer is what gets applied.
		A forged event can make this site ask Printful about an order, never change one.
		"""
		event = frappe.parse_json(payload.decode())
		order_ref = cstr(((event.get("data") or {}).get("order") or {}).get("id"))
		# Goes into a URL path, so anything but an id is refused.
		if not order_ref.isdigit():
			return {}

		tracking = self.read_order(order_ref)
		if not tracking["status"]:
			return {}
		return {
			**tracking,
			"tracking_ref": order_ref,
			"event_id": f"{event.get('type')}:{event.get('created')}",
		}

	# Printful collects from its own facilities and prints its own paperwork, so schedule_pickup and
	# generate_manifest are deliberately not implemented, and `supports()` reports False for them.

	# --- Helpers -------------------------------------------------------------

	def read_order(self, order_ref: str | None) -> dict:
		"""The order's status and, once it has shipped, the first package's AWB, carrier and link."""
		if not order_ref:
			return {"status": None, "provider_status": None, "events": []}

		order = self.request("GET", f"/orders/{order_ref}") or {}
		shipments = order.get("shipments") or []
		first = shipments[0] if shipments else {}
		return {
			"status": PRINTFUL_STATUS_MAP.get(order.get("status")),
			"provider_status": order.get("status"),
			"events": [build_event(shipment) for shipment in shipments],
			"awb": cstr(first.get("tracking_number")) or None,
			"carrier": first.get("carrier"),
			"tracking_url": first.get("tracking_url"),
		}

	def build_order(self, shipment: dict) -> dict:
		items = get_items(shipment.get("parcels"))
		if not items:
			frappe.throw(_("Printful prints each item itself, so a shipment with no items cannot be booked"))
		return {
			# Unique per Printful store, which is also what stops a retry creating a second order.
			"external_id": cstr(shipment.get("reference")),
			"shipping": shipment.get("service_code"),
			"recipient": self.build_recipient(shipment.get("destination") or {}),
			"items": [build_order_item(item) for item in items],
		}

	def build_recipient(self, address: dict) -> dict:
		country = address.get("country")
		country_code = cstr(frappe.db.get_value("Country", country, "code")).upper() if country else ""
		payload = {
			"name": address.get("contact_name"),
			"company": address.get("company_name"),
			"address1": address.get("line1"),
			"address2": address.get("line2"),
			"city": address.get("city"),
			"state_code": state_code(country_code, address.get("state")),
			"country_code": country_code,
			"zip": address.get("pincode"),
			"phone": address.get("phone"),
			"email": address.get("email"),
		}
		return {key: value for key, value in payload.items() if value not in ("", None)}

	def get_currency(self) -> str:
		return self.currency or DEFAULT_CURRENCY

	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Prove the token works and name the stores it can order from."""
		stores = self.request("GET", "/stores") or []
		return {"stores": [{"id": store.get("id"), "name": store.get("name")} for store in stores]}


def get_items(parcels: list[dict] | None) -> list[dict]:
	return [item for parcel in parcels or [] for item in parcel.get("items") or [] if item.get("sku")]


def build_rate_item(item: dict) -> dict:
	return {
		"external_variant_id": cstr(item.get("sku")),
		"quantity": cint(item.get("quantity")) or 1,
		"value": cstr(flt(item.get("rate"))),
	}


def build_order_item(item: dict) -> dict:
	return {
		"external_variant_id": cstr(item.get("sku")),
		"quantity": cint(item.get("quantity")) or 1,
		"retail_price": cstr(flt(item.get("rate"))),
		"name": cstr(item.get("description") or item.get("sku")),
	}


def build_event(shipment: dict) -> dict:
	return {
		"timestamp": to_system_datetime(shipment.get("shipped_at") or shipment.get("ship_date")),
		"status": "Shipped",
		"location": None,
		"message": " ".join(
			cstr(part)
			for part in (shipment.get("carrier"), shipment.get("service"), shipment.get("tracking_number"))
			if part
		),
		"event_id": f"shipment:{shipment.get('id')}",
	}


def state_code(country_code: str, state: str | None) -> str | None:
	"""Printful wants the ISO 3166-2 code ("CA"), and an ERPNext Address holds whatever was typed."""
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


def read_printful_error(body: dict) -> str | None:
	error = body.get("error") or {}
	return cstr(error.get("message") or body.get("result")) or None


def summarise(body: dict) -> dict:
	"""Printful echoes the customer's name, address and phone number, so only ids are logged."""
	result = body.get("result")
	if not isinstance(result, dict):
		return {}
	return {key: result[key] for key in ("id", "external_id", "status") if key in result}
