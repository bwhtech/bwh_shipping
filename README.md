<div align="center" markdown="1">

<img src="bwh_shipping/public/images/bwh_shipping.svg" alt="BWH Shipping logo" width="80" />
<h1>BWH Shipping</h1>

<a href="https://buildwithhussain.com"><img src=".github/built-at-bwh.svg" alt="Built at BWH" height="28" /></a>

**Rates, labels and tracking for Frappe and ERPNext — one contract, many carriers**

<p>
	<img src=".github/logos/shiprocket.svg" alt="Shiprocket" height="40" />
	<img src=".github/logos/aftership.svg" alt="AfterShip" height="40" />
</p>

</div>

## BWH Shipping

A storefront or a desk user asks for rates, books a consignment and reads tracking without ever knowing
which carrier is behind it. Adding a carrier is one Single DocType implementing five methods — checkout
pricing, the webhook, the status ladder and the desk stay exactly as they were.

📖 **[Developer docs](https://bwhdocs.fsn.frappe.cloud/bwh-shipping/get-started/overview)**: quote at
checkout, book shipments, add your own carrier, and set up each built-in one.

### Carriers

- **Shiprocket** — Courier aggregator for domestic India, with pincode serviceability, pickup scheduling
  and manifests.
- **AfterShip** — Global labels and tracking across hundreds of carriers, in a single booking call.

### Key Features

- **Delivery options you control.** A `Shipping Service` is what a shopper actually picks: its own title,
  markup, handling fee and optional Shipping Rule. An option that nothing can price is *hidden* at
  checkout rather than rendered as an accidental "Free".

- **A status ladder that cannot go backwards.** Carriers replay webhooks and deliver scans out of order,
  so provider statuses are ranked: one applies only if it ranks strictly higher than what is stored, and
  Delivered, Cancelled and Lost are terminal. Nothing can un-deliver a delivered order.

- **Bookings that can be resumed.** A carrier that creates an order and then fails before the waybill is
  *resumed* on retry, instead of quietly producing a second consignment.

- **Every provider quotes from its own pickup address.** One shared origin breaks the moment two carriers
  ship from different countries — an Indian carrier handed a US origin returns nothing, and every option
  silently drops to its backup charge.

- **One webhook endpoint for every carrier.** Signed where the carrier signs, token-checked where it does
  not, and answering a single opaque error for a bad signature or an unknown provider alike.

- **Fulfilment from ERPNext.** Draft a shipment straight from a Delivery Note; parcels, AWB, label, cost
  and tracking events all live on the `Shipping Request`.

- **Canonical units at the boundary.** Weight in kilograms, dimensions in centimetres, money in major
  units of the currency each amount names — including volumetric weight.

### Installation

BWH Shipping needs ERPNext, on Frappe 16 or later.

```bash
bench get-app https://github.com/bwhtech/bwh_shipping
bench --site your.site install-app bwh_shipping
```

Then fill in a carrier's settings, create a `Shipping Provider Profile` for it, and add the Shipping
Services shoppers can pick. Each carrier's setup is in the
[docs](https://bwhdocs.fsn.frappe.cloud/bwh-shipping/carriers/shiprocket).

### Adding a carrier

Subclass `ShippingProviderBase` on a Single DocType in your own app, and implement `get_rates`,
`create_shipment`, `cancel_shipment`, `get_tracking` and `handle_webhook`. Four more are optional —
pickups, manifests, resuming a partial booking and importing services — and callers ask
`supports("pickup" | "manifest" | "resume" | "service_choices")` rather than hard-coding which carrier can
do what. The [step-by-step guide](https://bwhdocs.fsn.frappe.cloud/bwh-shipping/build/build-a-carrier)
builds one from scratch, tests included.

### Under the Hood

- [Frappe Framework](https://github.com/frappe/frappe) — Full-stack Python web framework.
- [ERPNext](https://github.com/frappe/erpnext) — Address, Currency, Shipping Rule and Delivery Note.

## About BWH Studios

BWH Shipping is developed and maintained by BWH Studios, a tech company based in Jagdalpur, Chhattisgarh,
specializing in Frappe customizations and consulting.

#### License

MIT
