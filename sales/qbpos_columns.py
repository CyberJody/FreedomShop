# SPDX-License-Identifier: AGPL-3.0-or-later
"""QuickBooks POS export headings, not backup/SDK field names.

Sources (checked 2026-11-04): Intuit's Point of Sale 7.0 User's Guide,
chapter 8 (Exporting a List/Report to Excel), and Point of Sale 2013 User
Guide: Export Point of Sale List Data, Customizing Field Labels, Customer
Fields, Vendor Fields, Inventory and Sales Tax Codes. The latter documents
Item Name/Item Description replacing Description 1/Description 2. Templates
and field labels are configurable: these are aliases, not a universal schema.

https://http-download.intuit.com/http.intuit/OpenCms/sites/default/QBSupportSite/PDFs/POS70UG.pdf
https://blackrockbusiness.com/wp-content/uploads/2015/12/POS_2013_User_Guide.pdf

See docs/MIGRATE-FROM-QUICKBOOKS-POS.md for source sections and limitations.
"""
import re


def heading(value):
    return re.sub(r'[^a-z0-9#]', '', str(value or '').casefold().replace('%', 'percent'))


CONTACT = {
    'company': ('Company', 'Company Name'),
    'phone': ('Phone', 'Phone 1', 'Phone Number'),
    'email': ('Email', 'E-Mail', 'E-Mail Address'),
    'street': ('Address', 'Address 1', 'Street', 'Street Address'),
    'street2': ('Address 2', 'Street 2'),
    'city': ('City',), 'state': ('State', 'State/Province'),
    'postal_code': ('Zip', 'Zip Code', 'Postal Code'),
}
ALIASES = {
    'items': {
        'name': ('Item Name', 'Description 1', 'Description1'),
        'description': ('Item Description', 'Description 2', 'Description2'),
        'sku': ('Item #', 'Item Number', 'Item No', 'SKU'),
        'department': ('Dept Name', 'Department', 'Department Name'),
        'department_code': ('Dept Code', 'Department Code'),
        'vendor_code': ('Vendor Code', 'Vendor'),
        'attribute': ('Attribute', 'Attr'), 'size': ('Size',),
        'price': ('Regular Price', 'Price', 'Price 1'),
        'cost': ('Average Unit Cost', 'Avg Unit Cost', 'Avg. Cost', 'Average Cost'),
        'order_cost': ('Order Cost',),
        'quantity': ('On-Hand Qty', 'On Hand', 'On-Hand Quantity', 'Qty On Hand'),
        'upc': ('UPC', 'UPC Code'), 'alu': ('Alternate Lookup', 'ALU'),
        'tax_code': ('Tax Code', 'Tax Category'),
        'item_type': ('Item Type', 'Type'), 'unit_of_measure': ('Unit of Measure', 'UOM'),
    },
    'customers': {
        **CONTACT,
        'first_name': ('First Name',), 'last_name': ('Last Name',),
        'full_name': ('Customer Name', 'Full Name', 'Name'),
        'customer_number': ('Customer #', 'Customer Number'),
        'tax_exempt': ('Tax Exempt', 'Tax-Exempt'),
        'mobile': ('Mobile', 'Cell Phone'), 'notes': ('Notes',),
    },
    'vendors': {
        **CONTACT, 'code': ('Vendor Code', 'Code'),
        'contact': ('Contact', 'Contact Name'), 'fax': ('Fax',),
        'account_number': ('Account #', 'Account Number'), 'terms': ('Terms',),
    },
    # Report columns depend on the selected report. We require explicit line
    # extensions and line tax; receipt summary rows must not be mixed with lines.
    'sales': {
        'receipt': ('Receipt #', 'Receipt Number', 'Receipt No'),
        'date': ('Date', 'Receipt Date', 'Sales Date'),
        'customer': ('Customer', 'Customer Name'),
        'quantity': ('Qty', 'Quantity'), 'item': ('Item', 'Item Name', 'Description 1'),
        'attribute': ('Attribute',), 'size': ('Size',),
        'price': ('Price', 'Unit Price'),
        'extension': ('Ext Price', 'Ext. Price', 'Extended Price', 'Amount'),
        'tax': ('Tax', 'Sales Tax', 'Tax Amount'),
        'tax_rate': ('Tax Rate', 'Tax %', 'Tax Percent'),
        'tender': ('Tender', 'Payment Method', 'Tender Type'),
        'tender_amount': ('Tender Amount', 'Payment Amount'),
    },
}
LOOKUPS = {kind: {heading(alias): key for key, aliases in fields.items() for alias in aliases}
           for kind, fields in ALIASES.items()}
