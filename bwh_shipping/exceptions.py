import frappe


class PartialBookingError(frappe.ValidationError):
	"""A booking that got far enough to create something at the provider, then failed.

	Carries whatever handles the provider did hand back, so the caller can persist them before re-raising.
	Losing them means the next attempt creates a *second* order at the provider for one parcel, and nobody
	holds a reference to the first — the orphan is only discoverable by hand in the provider's dashboard.
	"""

	def __init__(self, message: str, booking: dict | None = None):
		super().__init__(message)
		self.booking = booking or {}
