# BWH Shipping

Shipping provider integrations for Frappe/ERPNext. One provider contract, many carriers.

The shipping sibling of [`bwh_payments`](https://github.com/Rl0007/bwh_payments): a storefront or desk asks
for rates, books a consignment and reads tracking without knowing which carrier is behind it. Adding a
provider is one Single doctype implementing five methods — nothing in checkout pricing, the webhook, the
status ladder or the desk changes.

## What's in it

| Doctype | Role |
|---|---|
| **Shipping Provider Profile** | Names an enabled provider and validates that its settings Single really implements the contract |
| **Shipping Request** | One consignment: parcels, AWB, label, cost, status, tracking events. Row-locked and idempotent |
| **Shipping Service** | A customer-facing delivery option — title, markup, handling fee, backup charge, optional Shipping Rule |
| **Shiprocket Shipping Settings** | Provider: India-domestic aggregator |
| **AfterShip Shipping Settings** | Provider: global aggregator (Postmen labels + AfterShip tracking) |

Plus `pricing.py` (the checkout pricing engine), `status.py` (the canonical status ladder), `units.py`
(kg/cm and volumetric weight), `webhook.py` (one guest endpoint for every provider) and `fulfilment.py`
(draft a shipment from a Delivery Note).

## The contract

Implement `bwh_shipping.base_class.ShippingProviderBase` on a `<Provider> Shipping Settings` Single:

```python
get_rates(origin, destination, parcels, cod, declared_value) -> list[dict]
create_shipment(shipment) -> dict
cancel_shipment(order_ref, shipment_ref, awb) -> dict
get_tracking(awb, shipment_ref, tracking_ref) -> dict
handle_webhook(payload, headers) -> dict
```

Four more are optional, and callers discover them with `supports("pickup" | "manifest" | "resume")` rather
than hard-coding which provider can do what:

- `schedule_pickup` — ask the carrier to collect
- `generate_manifest` — the handover sheet
- `resume_booking` — finish a booking the provider already half-created

`supports()` checks whether the subclass overrode the method, so it cannot drift out of step with reality.

**Units at the boundary are canonical**: weight in kilograms, dimensions in centimetres, money in major
units of the currency each amount names.

## Design rules worth knowing before you change it

- **The status ladder is ranked, not a flat map** (`status.py`). A provider status applies only when it
  ranks *strictly higher* than what is stored, and Delivered/Cancelled/Lost are terminal. Carriers replay
  webhooks and deliver scans out of order; nothing may un-deliver a delivered order. Provider statuses
  that don't map are deliberately absent so they can neither advance nor close a shipment.
- **`UNPRICEABLE` is a sentinel, not a zero.** An option with no Shipping Rule band, no live rate and no
  backup charge is *hidden* at checkout, never rendered as an accidental "Free".
- **Each provider quotes from its own pickup address.** One shared origin breaks the moment two providers
  ship from different countries — an Indian carrier handed a US origin returns no rates, and every option
  silently drops to its backup charge.
- **Partial bookings are recoverable.** A provider that creates an order then fails before the waybill
  raises `PartialBookingError` carrying what it created; `book()` persists those handles outside the
  transaction and a retry resumes instead of creating a second consignment.
- **Booking is row-locked and idempotent.** Two concurrent bookings cannot both buy a label.
- **Webhooks answer one opaque 400** for a bad signature, an unknown provider or a missing one, so nobody
  can enumerate what a site has configured. A verified-but-replayed delivery still gets a 200, or the
  provider retries forever.

## Provider differences the contract absorbs

|  | Shiprocket | AfterShip |
|---|---|---|
| Coverage | India domestic | Global |
| Booking | 3 calls (order → AWB → label) | 1 call |
| Pickup / manifest | yes | no endpoint |
| Resume partial booking | yes | n/a |
| Webhook auth | static shared token | HMAC-SHA256 signed |
| Service identified by | courier id | shipper account + service type |
| Countries | pincode | ISO alpha-3 |

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app git@github.com:bwhtech/bwh_shipping.git --branch develop
bench --site <site> install-app bwh_shipping
```

Requires `frappe/erpnext` (Address, Currency, Shipping Rule, Delivery Note).

## Setting up a provider

1. Fill in the provider's settings Single (API credentials, pickup address, webhook secret) and tick
   **Enabled**. Run **Test Connection** — it names what the carrier actually knows about your account.
2. Create a **Shipping Provider Profile** pointing at that settings doctype and enable it.
3. Create **Shipping Service** rows — one per delivery option a customer sees. Always set a **Backup
   Charge**: an option nothing can price is hidden rather than shown free.
4. Point the provider's webhook at
   `/api/method/bwh_shipping.bwh_shipping.webhook.handle?provider=<Profile Name>`.

### Storefront integration

Consumers call `bwh_shipping.bwh_shipping.pricing`:

```python
quote_services(origin=None, destination, parcels, cart, cod=False)  # None origin = per-provider pickup
get_charge_amount(title, cart, quoted_amount=None)                  # what to actually bill
get_charge_account(title)                                           # where the fee posts
```

Price the option server-side and store the amount at selection time. A client that can name its own
delivery charge can ship for nothing.

## Contributing

```bash
cd apps/bwh_shipping
pre-commit install
```

Ruff (line length 110, tabs, double quotes) must pass before a diff goes up.

## License

MIT
