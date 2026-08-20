import frappe
from frappe import _
from frappe.utils.caching import redis_cache
from frappe.utils.data import cstr, flt

from bwh_shipping.bwh_shipping.utils import get_provider_controller

SERVICE_FIELDS = [
	"name",
	"title",
	"description",
	"provider",
	"service_code",
	"carrier",
	"markup_percent",
	"handling_fee",
	"backup_charge",
	"shipping_rule",
]

# Returned by price_service when no band, no live rate and no backup charge prices a service for this
# destination. A distinct sentinel rather than None or 0.0, so a caller cannot collapse "cannot be priced"
# into "free" — billing nothing for a service the customer selected is the failure this exists to prevent.
UNPRICEABLE = object()

SERVICES_CACHE_TTL_SECONDS = 60 * 60


@redis_cache(ttl=SERVICES_CACHE_TTL_SECONDS)
def get_enabled_services() -> list[dict]:
	# Read on every checkout render. Shipping Service.on_update/on_trash clear this.
	return frappe.get_all(
		"Shipping Service",
		filters={"enabled": 1},
		fields=SERVICE_FIELDS,
		order_by="creation asc",
	)


def find_service(title: str) -> dict | None:
	"""Look up a service by title whether or not it is still enabled.

	Payment has to be able to price a selection that was disabled mid-checkout, so this deliberately does
	not filter on `enabled`. `title` is the autoname, so this is a primary-key read.
	"""
	return frappe.db.get_value("Shipping Service", title, SERVICE_FIELDS, as_dict=True)


def quote_services(
	origin: dict,
	destination: dict,
	parcels: list[dict],
	cart: dict,
	cod: bool = False,
) -> list[dict]:
	"""Price every enabled Shipping Service for this cart.

	A service covered by a band of its Shipping Rule is priced by that band; one with no covering band is
	priced from the live carrier rate plus markup and handling, and falls back to its own Backup Charge. A
	service the destination does not support — no band, no live rate, no backup charge — is dropped, so it
	can never render as an accidental "Free" row.

	Never raises: checkout must always render something.
	"""
	services = get_enabled_services()
	if not services:
		return []

	quotes = get_live_quotes(services, origin, destination, parcels, cart, cod)
	rows = [price_row(service, quotes.get(quote_key(service)), cart) for service in services]
	return [row for row in rows if row is not None]


def quote_key(service: dict) -> tuple:
	return (service.get("provider"), cstr(service.get("service_code")))


def get_live_quotes(
	services: list[dict], origin: dict, destination: dict, parcels: list[dict], cart: dict, cod: bool
) -> dict:
	"""Live rates for every provider these services span, keyed by (provider, service_code).

	One call per provider rather than per service: providers rate-shop all their couriers in a single
	request, so per-service calls would multiply checkout latency for identical answers.
	"""
	if not (origin and destination and parcels):
		return {}

	quotes = {}
	for provider in dict.fromkeys(service["provider"] for service in services if service.get("provider")):
		for rate in get_provider_rates(provider, origin, destination, parcels, cart, cod):
			quotes[(provider, cstr(rate.get("service_code")))] = rate
	return quotes


def get_provider_rates(
	provider: str, origin: dict, destination: dict, parcels: list[dict], cart: dict, cod: bool
) -> list[dict]:
	# A carrier being slow, broken or unconfigured must degrade to backup pricing, never break checkout.
	try:
		return get_provider_controller(provider).get_rates(
			origin,
			destination,
			parcels,
			cod=cod,
			declared_value=flt(cart.get("declared_value")),
		)
	except Exception:
		frappe.log_error(title=f"{provider} checkout rates failed")
		return []


def price_row(service: dict, quote: dict | None, cart: dict) -> dict | None:
	priced = price_service(service, quote, cart)
	if priced is UNPRICEABLE:
		return None
	amount, is_live_rate = priced
	return {
		"title": service["title"],
		"description": cstr(service.get("description")),
		"provider": service["provider"],
		"service_code": cstr(service.get("service_code")),
		"amount": amount,
		# The storefront gates its "Free" label on this: any zero final amount is free.
		"is_free": not amount,
		"is_live_rate": is_live_rate,
	}


def price_service(service: dict, quote: dict | None, cart: dict):
	"""(amount, is_live_rate) for this service, or UNPRICEABLE when nothing prices it for this cart."""
	band = get_covering_band(service.get("shipping_rule"), cart)
	if band is not None:
		if band.free_shipping:
			return 0.0, False
		# Shipping Rule bands are in COMPANY currency, the same as ERPNext's
		# add_shipping_rule_to_tax_table assumes; convert back to what the cart is priced in.
		return flt(flt(band.shipping_amount) / get_conversion_rate(cart), 2), False

	live_amount = get_live_amount(service, quote, cart)
	if live_amount is not None:
		return live_amount, True

	backup_charge = flt(service.get("backup_charge"), 2)
	if backup_charge > 0:
		return backup_charge, False
	return UNPRICEABLE


def get_shipping_rule(shipping_rule: str | None):
	if not shipping_rule:
		return None
	try:
		rule = frappe.get_cached_doc("Shipping Rule", shipping_rule)
	except frappe.DoesNotExistError:
		return None
	return None if rule.disabled else rule


def get_covering_band(shipping_rule: str | None, cart: dict):
	"""The band of this rule that brackets the cart, or None.

	ERPNext's ShippingRule.apply brackets a Net Weight rule on total weight and every other rule on
	base_net_total, so a weight rule must never be matched against the cart's value.
	"""
	rule = get_shipping_rule(shipping_rule)
	if not rule:
		return None

	value = (
		flt(cart.get("weight"))
		if rule.calculate_based_on == "Net Weight"
		else flt(cart.get("base_net_total"))
	)
	for condition in rule.conditions:
		if flt(condition.from_value) <= value and (
			not condition.to_value or value <= flt(condition.to_value)
		):
			return condition
	# Deliberately no fallback band: a cart outside every band falls through to the Backup Charge rather
	# than shipping free. A rule that means "free above X" needs an open-ended top band saying so.
	return None


def get_conversion_rate(cart: dict) -> float:
	return flt(cart.get("conversion_rate")) or 1.0


def get_live_amount(service: dict, quote: dict | None, cart: dict) -> float | None:
	"""The marked-up live carrier amount, or None when there is no usable quote."""
	if not quote:
		return None

	amount = convert_to_cart_currency(flt(quote.get("amount")), quote.get("currency"), cart)
	if amount is None:
		# A quote in a currency we cannot convert today. Charging the number verbatim would bill rupees as
		# riyals, so the option falls back to its backup charge instead.
		return None

	marked_up = amount * (1 + flt(service.get("markup_percent")) / 100) + flt(service.get("handling_fee"))
	return flt(marked_up, 2)


def convert_to_cart_currency(amount: float, currency: str | None, cart: dict) -> float | None:
	cart_currency = cart.get("currency")
	if not currency or not cart_currency or currency == cart_currency:
		return amount

	# Imported lazily: this is the only line in the pricing engine that needs erpnext, and a rate lookup
	# failing must not stop the module loading.
	from erpnext.setup.utils import get_exchange_rate

	try:
		conversion = flt(get_exchange_rate(currency, cart_currency))
	except Exception:
		conversion = 0.0
	if not conversion:
		frappe.log_error(
			title="Shipping rate conversion failed",
			message=f"No exchange rate {currency} -> {cart_currency}",
		)
		return None
	return amount * conversion


def get_charge_amount(
	title: str,
	cart: dict,
	quoted_amount: float | None = None,
) -> float:
	"""What to actually bill for a chosen delivery option.

	`quoted_amount` is the figure the shopper was shown and is trusted only because the caller stored it
	server-side at selection time. Without one, the option is re-priced here — and a live-rate-only option
	has no live quote at this point, so it fails loudly rather than shipping for free.
	"""
	if quoted_amount is not None:
		return flt(quoted_amount, 2)

	service = find_service(title)
	if service is None:
		frappe.throw(
			_("Delivery option {0} no longer exists, and no quoted amount was stored for it.").format(title)
		)

	priced = price_service(service, None, cart)
	if priced is UNPRICEABLE:
		frappe.throw(
			_(
				"Delivery option {0} cannot be priced for this order. Set a Backup Charge on it, or a"
				" Shipping Rule band that covers this cart."
			).format(title)
		)
	amount, _is_live_rate = priced
	return flt(amount, 2)


def get_charge_account(title: str) -> str | None:
	"""The account a delivery fee should post against: the option's Shipping Rule account when it has one,
	else None so the caller falls back to its own default."""
	shipping_rule = frappe.db.get_value("Shipping Service", title, "shipping_rule")
	if not shipping_rule:
		return None
	return frappe.get_cached_value("Shipping Rule", shipping_rule, "account")
