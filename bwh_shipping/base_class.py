from abc import ABC, abstractmethod

# Optional capabilities, mapped to the method that implements them. Aggregators differ on what they
# expose — AfterShip has no pickup or manifest call at all, Shiprocket has both — so a caller asks
# `supports()` instead of hard-coding which provider can do what.
OPTIONAL_CAPABILITIES = {
	"pickup": "schedule_pickup",
	"manifest": "generate_manifest",
	"resume": "resume_booking",
}


class ShippingProviderBase(ABC):
	"""Contract every `<Provider> Shipping Settings` Single must satisfy to back a Shipping Provider Profile.

	Units crossing this boundary are canonical: weight in KILOGRAMS, dimensions in CENTIMETRES, money in
	MAJOR units of the currency each amount names. What a provider does with them is its own business —
	Shiprocket happens to bill in kg/cm/INR already, a US aggregator would convert with
	`bwh_shipping.units` — but a rate and the label bought against it go through the same conversion, so
	the quote and the invoice can never disagree.

	Addresses are dicts of `line1, line2, city, state, pincode, country, contact_name, company_name,
	phone, email`. Parcels are dicts of `length, width, height, weight, count` plus an optional `items`
	list; run them through `bwh_shipping.units.normalise_parcel` before they get here.
	"""

	@abstractmethod
	def get_rates(
		self,
		origin: dict,
		destination: dict,
		parcels: list[dict],
		cod: bool = False,
		declared_value: float = 0.0,
	) -> list[dict]:
		"""Quote every service that can carry this consignment.

		Return a list of {"service_code", "service_name", "carrier", "amount", "currency",
		"transit_days", "cod_available"}. Return [] when the provider serves neither end of the route —
		an empty quote is a normal answer at checkout, not an error, and the caller prices the option
		from its backup charge instead.
		"""

	@abstractmethod
	def create_shipment(self, shipment: dict) -> dict:
		"""Book one consignment and buy its label.

		Return {"order_ref", "shipment_ref", "awb", "carrier", "label_url", "cost_amount",
		"cost_currency", "status"}. `order_ref` and `shipment_ref` are whatever handles the provider
		needs later for cancel, pickup and manifest; both are stored verbatim.
		"""

	@abstractmethod
	def cancel_shipment(
		self, order_ref: str, shipment_ref: str | None = None, awb: str | None = None
	) -> dict:
		"""Cancel a booked consignment. Return {"status", "message"}."""

	@abstractmethod
	def get_tracking(self, awb: str, shipment_ref: str | None = None) -> dict:
		"""Read the authoritative status from the provider.

		Return {"status", "provider_status", "events"} where `status` is one of
		`bwh_shipping.status.SHIPMENT_STATUSES` and each event is {"timestamp", "status", "location",
		"message"}.
		"""

	@abstractmethod
	def handle_webhook(self, payload: bytes, headers: dict) -> dict:
		"""Verify the delivery is really from the provider, then return {} to ignore it, or
		{"awb", "status", "event_id", "events"}."""

	def schedule_pickup(self, shipment_ref: str, pickup_date: str | None = None) -> dict:
		"""Ask the carrier to collect. Return {"pickup_ref", "scheduled_date", "message"}."""
		raise NotImplementedError(f"{self.get_provider_name()} cannot schedule pickups")

	def generate_manifest(self, shipment_refs: list[str]) -> dict:
		"""Return {"manifest_url"} for the handover sheet covering these consignments."""
		raise NotImplementedError(f"{self.get_provider_name()} cannot generate manifests")

	def resume_booking(self, order_ref: str, shipment_ref: str, service_code: str | None = None) -> dict:
		"""Finish a booking that already exists at the provider, returning the same shape as
		`create_shipment`.

		Providers that book in stages implement this so a failure part-way through is recoverable. Without
		it a retry can only start again, which at a provider that books in stages means a second order for
		one parcel and an orphan nobody holds a reference to.
		"""
		raise NotImplementedError(f"{self.get_provider_name()} cannot resume a partial booking")

	def supports(self, capability: str) -> bool:
		"""Whether this provider actually implements an optional capability.

		Derived from whether the subclass overrode the method rather than from a per-provider list of
		capability strings: a list has to be kept in step by hand, and the day it drifts the desk offers
		a button that raises NotImplementedError.
		"""
		method_name = OPTIONAL_CAPABILITIES.get(capability)
		if not method_name:
			return False
		return getattr(type(self), method_name) is not getattr(ShippingProviderBase, method_name)

	def get_provider_name(self) -> str:
		return self.__class__.__name__
