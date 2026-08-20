import frappe
from frappe import _

# The provider-agnostic ladder a Shipping Request moves along. Every provider maps its own vocabulary
# into these; anything it reports that does not map is left out deliberately, so an unrecognised carrier
# state can neither advance a shipment nor close it.
SHIPMENT_STATUSES = (
	"Draft",
	"Ready To Ship",
	"Pickup Scheduled",
	"In Transit",
	"Out For Delivery",
	"Undelivered",
	"RTO",
	"Delivered",
	"Cancelled",
	"Lost",
)

# How far along the journey each status sits. Carriers routinely push scans out of order — a "picked up"
# event can arrive after an "in transit" one, and a webhook retry can replay yesterday's history — so a
# status only applies when it ranks strictly higher than what is stored. Delivered, Cancelled and Lost
# share the top rank because they are alternative endings, not successive ones.
STATUS_RANK = {
	"Draft": 0,
	"Ready To Ship": 1,
	"Pickup Scheduled": 2,
	"In Transit": 3,
	"Out For Delivery": 4,
	"Undelivered": 5,
	"RTO": 6,
	"Delivered": 7,
	"Cancelled": 7,
	"Lost": 7,
}

# Where a shipment stops. A later scan on one of these is a duplicate or a replay, and applying it would
# un-deliver a delivered order.
TERMINAL_STATUSES = ("Delivered", "Cancelled", "Lost")


def validate_status(status: str):
	if status not in STATUS_RANK:
		frappe.throw(_("{0} is not a known shipment status").format(frappe.bold(status)))


def can_advance(current: str, new: str) -> bool:
	"""Whether a provider-reported status may overwrite the stored one."""
	if new not in STATUS_RANK or new == current:
		return False
	if current in TERMINAL_STATUSES:
		return False
	return STATUS_RANK[new] > STATUS_RANK.get(current, 0)


def is_terminal(status: str) -> bool:
	return status in TERMINAL_STATUSES
