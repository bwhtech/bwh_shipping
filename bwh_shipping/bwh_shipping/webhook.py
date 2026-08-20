import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.rate_limiter import rate_limit

WEBHOOK_ACCEPTED = {"status": "ok"}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=120, seconds=60, ip_based=True)
def handle():
	"""Public provider callback. Everything here is untrusted until the provider's own check clears it."""
	provider = frappe.request.args.get("provider")
	if not provider:
		return reject()

	profile = frappe.db.get_value(
		"Shipping Provider Profile", provider, ["name", "enabled", "provider_settings"], as_dict=True
	)
	if not profile or not profile.enabled:
		return reject()

	payload = frappe.request.get_data()
	headers = dict(frappe.request.headers)

	try:
		result = frappe.get_single(profile.provider_settings).handle_webhook(payload, headers)
	except Exception:
		# The payload carries the customer's address and phone number, so only the provider and the
		# traceback are recorded.
		frappe.log_error(title=f"{provider} shipping webhook verification failed")
		log_webhook(provider, status="Failed")
		return reject()

	if not result:
		log_webhook(provider, status="Completed")
		return WEBHOOK_ACCEPTED

	awb = result.get("awb")
	status = result.get("status")
	event_id = result.get("event_id")

	if not (awb and status):
		log_webhook(provider, event_id=event_id, status="Failed")
		return WEBHOOK_ACCEPTED

	request_name = frappe.db.get_value("Shipping Request", {"awb": awb}, "name")
	if not request_name:
		log_webhook(provider, awb=awb, event_id=event_id, status="Failed")
		return WEBHOOK_ACCEPTED

	shipping_request = frappe.get_doc("Shipping Request", request_name)
	if shipping_request.provider != provider:
		frappe.log_error(
			title=f"{provider} shipping webhook provider mismatch",
			message=f"URL provider: {provider}, request provider: {shipping_request.provider}",
		)
		log_webhook(provider, awb=awb, event_id=event_id, status="Failed")
		return reject()

	apply_status_as_administrator(shipping_request, status, event_id, result.get("events"))
	log_webhook(provider, awb=awb, event_id=event_id, status="Completed")
	# A replayed delivery is still a success as far as the provider is concerned; anything else and it
	# keeps retrying forever.
	return WEBHOOK_ACCEPTED


def apply_status_as_administrator(shipping_request, status: str, event_id: str | None, events: list | None):
	# The callback arrives as Guest, but the downstream Sales Order write-back needs a real user context.
	# Restore the original user afterwards so a long-lived worker does not keep Administrator for the
	# next job.
	session_user = frappe.session.user
	try:
		frappe.set_user("Administrator")
		shipping_request.apply_webhook_status(status, event_id=event_id, events=events)
	finally:
		frappe.set_user(session_user)


def reject() -> dict:
	"""One opaque 400 for every verification failure.

	A caller who can tell "unknown provider" from "bad key" apart can enumerate which providers this site
	has configured and probe how far a forged payload gets. The detail stays in the Error Log and the
	Integration Request. Clearing the message log stops a `frappe.throw` raised inside a provider's
	verifier from being echoed back to the caller in `_server_messages`.
	"""
	frappe.local.message_log = []
	frappe.local.response["http_status_code"] = 400
	return {"status": "error", "message": _("Webhook could not be verified")}


def log_webhook(provider: str, awb: str | None = None, event_id: str | None = None, status: str = "Queued"):
	create_request_log(
		{"provider": provider, "awb": awb, "event_id": event_id},
		service_name=f"{provider} Shipping Webhook",
		status=status,
		reference_doctype="Shipping Request",
	)
