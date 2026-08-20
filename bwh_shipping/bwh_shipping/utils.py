import frappe
from frappe import _
from frappe.utils.caching import site_cache


@site_cache(ttl=60 * 60)
def get_available_shipping_providers() -> list[str]:
	# Read on every checkout render. Shipping Provider Profile.on_update/on_trash clear this, but
	# site_cache lives in the worker process, so a save only clears the worker that took it; the TTL is
	# what bounds how long the other workers keep quoting a just-disabled provider.
	return frappe.get_all("Shipping Provider Profile", filters={"enabled": 1}, pluck="name")


def resolve_provider(provider: str) -> str | None:
	"""Return the enabled Shipping Provider Profile matching a client-supplied name, case-insensitively."""
	requested = (provider or "").strip().casefold()
	if not requested:
		return None
	for profile in get_available_shipping_providers():
		if profile.casefold() == requested:
			return profile
	return None


def get_provider_controller(provider: str):
	"""The settings Single backing a profile, as a ShippingProviderBase."""
	settings = frappe.get_cached_value("Shipping Provider Profile", provider, "provider_settings")
	if not settings:
		frappe.throw(_("Shipping Provider Profile {0} has no settings doctype").format(frappe.bold(provider)))
	return frappe.get_single(settings)


def get_address_payload(address_name: str, contact_name: str | None = None) -> dict:
	"""One ERPNext Address in the shape ShippingProviderBase expects."""
	if not address_name:
		frappe.throw(_("An address is required to quote or book a shipment"))
	address = frappe.get_cached_doc("Address", address_name)
	return {
		"contact_name": contact_name or address.address_title,
		"company_name": address.address_title,
		"line1": address.address_line1,
		"line2": address.address_line2,
		"city": address.city,
		"state": address.state,
		"pincode": address.pincode,
		"country": address.country,
		"phone": address.phone,
		"email": address.email_id,
	}


def get_default_origin(provider: str) -> dict | None:
	"""Where a checkout quote ships from: the provider's own configured pickup address.

	By convention a provider's settings Single exposes a `pickup_address` Address link. It is read by
	convention rather than declared on the contract because it is configuration, not behaviour — a
	provider with no such field simply has no default origin, and the caller quotes from backup charges.
	"""
	address_name = getattr(get_provider_controller(provider), "pickup_address", None)
	if not address_name:
		return None
	return get_address_payload(address_name)
