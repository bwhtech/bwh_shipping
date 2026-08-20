# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from bwh_shipping.bwh_shipping.pricing import get_enabled_services


class ShippingService(Document):
	def validate(self):
		self.validate_bookable()

	def validate_bookable(self):
		# An enabled service is buyable at checkout, so a gap has to surface here, at config time, rather
		# than on a live order that has already taken the customer's money.
		if not self.enabled:
			return
		if not self.service_code:
			frappe.throw(
				_(
					"Set a Service Code on {0} before enabling it — a label cannot be booked without one."
				).format(frappe.bold(self.title))
			)
		if not frappe.get_cached_value("Shipping Provider Profile", self.provider, "enabled"):
			frappe.throw(
				_("Shipping Provider Profile {0} is disabled, so {1} cannot be offered at checkout.").format(
					frappe.bold(self.provider), frappe.bold(self.title)
				)
			)

	def on_update(self):
		get_enabled_services.clear_cache()

	def on_trash(self):
		get_enabled_services.clear_cache()
