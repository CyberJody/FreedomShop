# Custom item labels and receiving

FreedomShop has two different identifiers:

| Identifier | What it identifies | Receiving workflow |
| --- | --- | --- |
| Catalog item number, UPC or alternate lookup (ALU) | A product that can have many units in stock | Item lookup on a receiving voucher, followed by quantity entry |
| PO piece serial | One physical piece ordered on a purchase-order line | Scan the piece label in Truck Receiving |

Piece serials supplement catalog item numbers. They do not replace them. The app tracks the labeled PO pieces through receiving; this is not manufacturer serial tracking through later sales or returns. The item's **Use serial numbers** checkbox is stored for reference and does not control PO piece labels.

## Custom products without supplier barcodes

This is useful for made-to-order furniture and other goods from vendors that do not supply barcodes.

1. Create and save the purchase order. Confirm the item, description and quantities for each ordered product.
2. Print the PO's piece labels. FreedomShop allocates one unique serial per ordered piece and retains those serials on reprints. Send the labels to the vendor to attach to the matching pieces before shipping.
3. Open **Truck Receiving**. Scan the QR address or enter the piece serial in its scan box. Use a 2D scanner in keyboard mode that sends Enter. The first valid scan starts the truck session.
4. Check the scan feedback. The app finds the PO line, groups the load by vendor and PO, and flags repeated or invalid scans. Remove an incorrect scan before receiving.
5. Choose **Receive Items** after checking the load. The app creates one receiving voucher per PO and updates stock. Pieces that were not scanned remain due. Scanning alone does not post inventory.

Keep the current Truck Receiving screen dedicated to these unique piece labels. A supplier's shared product barcode does not identify a specific PO piece and cannot be substituted for its serial.

## Products with existing supplier barcodes

The current application already supports catalog lookup using UPC and ALU, including alternate-vendor codes.

1. Add or edit the catalog item and save its supplier code in **UPC** or **ALU**. Preserve leading zeroes. Use the alternate-vendor rows for that vendor's codes when appropriate.
2. On a receiving voucher, focus the item search and scan the barcode. Choose the correct match from the results; searching does not guarantee that a code is unique across your catalog.
3. Enter the received quantity and verify the cost. When receiving against a PO, check the source PO line and quantities as well. Existing PO-linked rows can already be populated from the order.
4. Receive the voucher to update stock. Use the normal held-voucher and reversal workflows when needed.

An unknown barcode does not fetch product details from an external catalog, create an item or add stock. Add and verify the catalog item first. This is a lookup-and-quantity workflow, not a scan-each-unit counting screen.

## Your public QR website address

A piece-label QR can do two jobs: a phone camera opens your shop's public website, and the receiving scanner extracts the serial from the same address. The website can show your brand, care instructions, collections or contact information. You provide that content; FreedomShop does not publish product pages or private customer/order information there.

The public page does not need access to the POS. Keep FreedomShop on a trusted LAN with private VPN access for remote staff, as described in [INSTALL.md](../INSTALL.md#network-access). Do not point the public label hostname at the private POS server.

1. Choose a page on your existing public website. A dedicated subdomain, such as `labels.example.com`, is optional; it gives printed labels an address you can keep even if your main website changes.
2. If using a subdomain, add it as a supported hostname at your website host. Follow that host's DNS instructions and enable HTTPS. A CNAME contains a hostname, without a scheme or page path. Adding DNS alone does not create a page, a redirect or a working HTTPS virtual host.
3. In **File → Business Settings → QR label website address** (called **Item label web address** in earlier builds), enter `https://labels.example.com/?wc={code}`, replacing the hostname with yours. This is separate from the general **Website** field. A page path is also fine, for example `https://www.example.com/care/?wc={code}`. Keep `wc={code}` in the query string: receiving extracts the serial from that parameter, not from a custom URL path. If the setting is blank, the server setting is used; configure and test a real shop address before sending labels to vendors.
4. Generate one test label. Confirm that a phone opens the intended public page over HTTPS and that the receiving scanner recognizes the piece code. Test receiving against generated records on a test installation.

If the website redirects, preserve the query string, including `wc`. The page can ignore the serial and show ordinary marketing content; the private POS still knows which PO piece the label identifies. A serial is a printed identifier, not a password or permission to view private records.

Keep the hostname working for as long as the physical labels may be scanned. Updating the label address affects newly generated labels, including reprints, while preserving the piece serial. Labels already printed retain their old address.

### Cloudflare Pages

For an existing website hosted on Cloudflare Pages:

1. Open **Workers & Pages → your Pages project → Custom domains → Set up a domain**.
2. Add the label subdomain and follow the DNS instructions Cloudflare shows.
3. Associate the hostname with the Pages project before adding a CNAME manually. Cloudflare documents that a CNAME by itself can fail when the hostname has not been associated with the project.
4. Confirm the public page and HTTPS work before configuring the label address and printing labels.

See [Cloudflare Pages custom domains](https://developers.cloudflare.com/pages/configuration/custom-domains/). The same domain-registration and DNS steps apply only to the public website, not the private FreedomShop installation.

### Cloudflare redirects

For a label hostname that redirects to an existing public page, use a redirect rule limited to that hostname. Enable **Preserve query string** so the `wc` serial reaches the destination. Cloudflare Single Redirects require the incoming hostname to be proxied. Check the complete HTTPS request and final address before printing labels.

Follow [Cloudflare's redirect setup instructions](https://developers.cloudflare.com/rules/url-forwarding/single-redirects/create-dashboard/). FreedomShop does not currently create DNS records or Cloudflare rules. Manual setup is sufficient for this feature; built-in Cloudflare automation would be separate work.

## Planned Barcode Receiving screen

The agreed next enhancement is a **separate screen** for ordinary supplier barcodes, keeping the fast, serialized Truck Receiving workflow unchanged. It should offer both:

- **Scan each unit:** repeated scans of the product barcode build the received count.
- **Scan once, enter quantity:** scan a product and enter the number delivered.

Implementation still needs explicit handling for unknown or ambiguous codes, multiple open PO lines for the same item, quantities above what is due, held work, corrections and concurrent users. Stock should change only when the checked receiving voucher is posted. A repeated ordinary barcode can represent another unit; a repeated unique piece serial must remain a duplicate in Truck Receiving.

This screen is planned, not implemented. Do not add a barcode-mode selector, fallback lookup or extra prompt to the current Truck Receiving scan path.
