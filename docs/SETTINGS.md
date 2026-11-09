# Settings

## Business website and QR labels

Admins open **File → Business Settings** (`/settings/business/`). These are separate saved settings:

| Setting | Purpose |
| --- | --- |
| Website | General website address in the shop's business details. Changing it does not change the QR destination. |
| QR label website address | Public address encoded in PO piece-label QR codes, for example `https://labels.example.com/?wc={code}`. Earlier builds label this **Item label web address**. Keep `wc={code}` in the query string so Truck Receiving can extract the serial. |
| Serial prefix | Shop-selected prefix for newly generated piece serials. Existing serials keep their prefix. |

The QR address is stored per installation as `Business.label_url`. A blank value falls back to the server's `PIECE_LABEL_URL`, then the placeholder `https://example.com/?wc={code}`. Set a real public website address before distributing labels. Updating this setting changes newly generated labels and reprints, not labels already printed. It does not change the piece serials or the Truck Receiving workflow.

The public page can be on the shop's existing website; a dedicated subdomain and Cloudflare are optional. It does not need access to the private POS. See [Custom labels and receiving](CUSTOM-LABELS.md#your-public-qr-website-address) for setup and testing instructions.

## Register and sales rules

Admins open **File → Register & Sales Rules** (`/settings/register/`), also linked from Business Settings. Every option is a checkbox except the discount percentage. Changes apply on the next request. A policy database read failure stops the request rather than granting default rights. Admins always have all register rights; a cashier PIN uses the shared register login's rights.

| Setting | Existing reference install | New shop |
| --- | --- | --- |
| Require a customer on every sale | Yes | No |
| Edit inventory prices | Yes | No |
| Edit costs | Yes | No |
| See costs | Yes | No |
| Adjust stock | Yes | No |
| Add or edit items | Yes | No |
| Add or edit vendors | Yes | No |
| Add or edit departments | Yes | No |
| Turn on charge accounts and set limits | Yes | No |
| Give receipt discounts or edit receipt prices | Yes | Yes |
| Require manager approval above a discount percentage | No | No |
| Discount threshold | 0% | 0% |
| Require a discount reason | Yes | No |

Migration `shop_settings.0010_register_rules` writes explicit saved values. An existing install has a Business row or sales/purchasing documents before that migration. Other databases get the new-shop values. Run setup again does not reset these rules or add a setup step.

Cash/card/gift-certificate sales and cash refunds can be walk-ins when the general customer requirement is off. Account tender, payments on account, store credit, orders, layaways, holds and deposits still require a customer. Walk-in receipts omit the customer block. Deposits use the existing customer-order payment workflow; store credit uses Account.

Cost visibility covers HTML, item-list JSON batches, lookup JSON, item details, adjustment costs and cost-derived statistics. Purchase documents, their PDF/print routes and Truck Receiving require cost visibility. Cost entry on a populated item form also requires cost visibility. Editing a purchase document also requires cost editing; receiving and Truck Receiving require stock adjustment rights. Sales and returns still change stock as part of their normal transaction. There is no separate physical-count screen in this version; manual stock entry is on the item form.

A discounted receipt or line above the threshold needs an admin's username and password. A price below regular counts as a discount; combined line and receipt reductions are considered. At 0%, any discount needs approval when enabled. Without the receipt-discount right, any receipt price edit (including an increase), miscellaneous/unpriced item price entry or discount needs approval. The editor keeps those controls available for a task approved through Manager Override. Other forbidden editing controls are omitted.

The server authenticates the admin without signing them in. An approval lasts two minutes, works once, and is bound to the register session, document, submitted contents and computed amounts. It is consumed in the document transaction; a failed save leaves it usable for the identical retry until expiry. The document and relevant lines record the approving user. Each credential attempt, refusal, busy/rate-limit response and approval is recorded in `accounts.AuditEntry`; passwords and raw approval tokens are never audited. Five failed approvals in fifteen minutes block further attempts for both that register session and that admin name, including attempts from another register. Counters use the shared `register_overrides` database cache, created by migrations.

The reason checkbox gates the existing receipt/order reason check and checks new manual line discounts through the editor/API. Historical unchanged line discounts, imports and pickups can retain blank reasons. Imports and internal fulfillment services do not manufacture new manual-discount approvals; ordinary HTTP editor saves always run the authorizer.

## Compatibility verification

Run the whole suite in the quick test mode with explicit saved reference values:

```sh
python manage.py test --settings=config.reference_profile_test_settings --noinput
```

`config.test_settings` also seeds the historical policy for existing tests. New server tests explicitly save new-shop values and authenticate real register/admin identities. The anonymous-view flag exists only in the test settings to accommodate the existing suite's middleware-free requests; production settings do not enable it. No existing test file is edited for the reference run. PostgreSQL validation uses the project's separate PostgreSQL test settings and is performed by the release lead/reviewer.

## Server enforcement points

| View / endpoint | Right or rule |
| --- | --- |
| `shop_settings.views.register_rules_view` — `/settings/register/` | Active Admin only; 403 for other identities, including GET |
| `purchasing.item_views.item_edit` — item new/edit/copy | `edit_items`; submitted regular/alternate price fields: `edit_prices`; cost fields: `edit_costs`; on-hand field: `adjust_stock`; cost field rendering: `see_costs` |
| `purchasing.item_views.item_name_check` — item name-check JSON | `edit_items` |
| `purchasing.item_views.special_order_item` — special-order-item JSON | `edit_items`, `edit_prices`; a supplied cost also requires `edit_costs` |
| `purchasing.views.catalog_edit` — vendor/department new/edit/copy | `edit_vendors` / `edit_departments`; items delegate to `item_edit` |
| `config.records.remove_record` — item/vendor/department delete | Corresponding `edit_items` / `edit_vendors` / `edit_departments`; item trash retains its existing Admin gate |
| `purchasing.views.catalog`, `purchasing.item_scrolling.item_rows`, `purchasing.item_views.item_details`, `purchasing.item_views.item_edit` | `see_costs` controls cost values and derived profit figures; denied values are absent from responses |
| `purchasing.views.item_lookup`, `purchasing.item_picker.item_picker` — purchasing lookup/picker JSON | Omit the `cost` key without `see_costs` |
| `purchasing.views.editor` — purchase order / voucher new/edit/copy and POST | `see_costs`, `edit_costs`; vouchers also require `adjust_stock` |
| `purchasing.views.document_list`, `document_pdf`, `document_print` — purchase history/orders/held and document output | `see_costs` |
| `purchasing.truck_views.truck_list`, `truck_new`, `truck_detail`, `truck_scan`, `truck_state`, `truck_remove`, `truck_receive`, `truck_cancel` | `see_costs`, `edit_costs`, `adjust_stock`, plus existing login checks |
| `customers.views.customer_edit` — customer new/edit/copy | Forged charge-account/limit fields require `manage_charge_accounts`; forbidden controls omitted; no automatic charge account for a new customer without the right |
| `customers.views.charge_account` — charge-account JSON | `manage_charge_accounts` |
| `sales.views.editor` — sale/order editor POST | Computed line/receipt discounts and price edits: `receipt_discounts` or single-use manager approval; percentage threshold; discount reasons; `override_limit` also requires `manage_charge_accounts`; indirect special-item catalog edits require `edit_items` |
| `sales.views.order_action` — order payment POST | `override_limit` requires `manage_charge_accounts`; deposits and fulfillment require a customer, including older documents |
| `sales.manager_override.manager_override` — manager-override POST | Active register login; valid active Admin credentials; shared failure limits; supported kind, document binding and server validation |
| `sales.services.save_document`, reached by editor and fulfillment | Saved customer rule plus unconditional customer requirements (also enforced at `sales.views.order_action` and `sales.pickup.ineligible_reason`); existing reason check gated by setting; authorizer runs before editor stock/document writes |
| Existing report, import, trash mutation and printer-configuration views | Existing Admin gates remain in force; Admin always has every register right |
