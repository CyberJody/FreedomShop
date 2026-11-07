# SPDX-License-Identifier: AGPL-3.0-or-later
from customers.views import customer_list, customer_edit, charge_account, customer_contact
from sales import views as sales
from sales import printer_views
from purchasing import views as purchasing
from purchasing import item_views
from purchasing.reorder import reminder_count
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render, redirect
from urllib.parse import urlencode
from django.urls import include, path
from django.views.decorators.http import require_safe


@require_safe
def home(request):
    return render(request, "home.html", {
        "sales_tasks": [
            {"label": "Customer Orders", "url": "/pos/order/new/", "icon": "customer-order", "flow": "right down"},
            {"label": "Make a Sale", "url": "/pos/sale/new/", "icon": "register", "flow": "right down"},
            {"label": "Sales History", "url": "/pos/history/", "icon": "sales-history"},
            {"label": "Customer List", "icon": "customers", "url": "/customers/"},
            {"label": "Order List", "url": "/pos/orders/", "icon": "order-list"},
            {"label": "Held Receipts", "url": "/pos/held/", "icon": "held-receipts"},
            {"label": "Awaiting Pickup", "url": "/pos/awaiting-pickup/", "icon": "awaiting-pickup"},  # under Sales History
            {"label": "Tag Generator", "url": "/tags/", "icon": "tag-generator", "flow": "col-4"},  # under Customer List
        ],
        "purchasing_tasks": [
            {"label": "Purchase Orders", "url": "/purchasing/po/new/", "icon": "purchase-order", "flow": "right down"},
            {"label": "Receiving Vouchers", "url": "/purchasing/voucher/new/", "icon": "truck", "flow": "right down"},
            {"label": "Receiving History", "url": "/purchasing/history/", "icon": "receiving-history"},
            {"label": "Item List", "url": "/purchasing/items/", "icon": "scanner"},
            {"label": "PO List", "url": "/purchasing/orders/", "icon": "po-list"},
            {"label": "Held Vouchers", "url": "/purchasing/held/", "icon": "held-vouchers"},
            {"label": "Vendor List", "url": "/purchasing/vendors/", "icon": "vendor"},
            {"label": "Department List", "url": "/purchasing/departments/", "icon": "folder"},
            {"label": "Truck Receiving", "url": "/purchasing/truck/", "icon": "truck-scan"},  # scan pieces off the truck
        ],
        "report_tasks": [
            {"label": "General Sales", "url": "/reports/general-sales/", "icon": "reports"},
            {"label": "Sales by Vendor", "url": "/reports/sales-by-vendor/", "icon": "reports"},
            {"label": "Sales Tax", "url": "/reports/sales-tax/", "icon": "reports"},
            {"label": "Daily Totals", "url": "/reports/daily-totals/", "icon": "reports"},
            {"label": "FreedomBooks exchange", "url": "/reports/exchange/", "icon": "reports"},
            {"label": "Inventory On Hand", "url": "/reports/inventory-on-hand/", "icon": "reports"},
            {"label": "Customer Balances", "url": "/reports/customer-balances/", "icon": "reports"},
            {"label": "All Reports", "url": "/reports/", "icon": "reports"},
            {"label": "Server Status", "url": "/server-status/", "icon": "server-status"},
            {"label": "Scanner Test", "url": "/scanner-test/", "icon": "server-status"},
            {"label": "Business Settings", "url": "/settings/business/", "icon": "reports"},
            {"label": "Tax Settings", "url": "/settings/tax/", "icon": "reports"},
        ],
        "reorder_count": reminder_count(),  # Reorder Reminders message on the Navigator (QB "You have inventory reminders")
        "shortcuts": ["Make a Sale", "Receive Items", "Item List", "Customer List", "Department List", "Sales Order", "Notes & Requests"],  # last one gold
    })


@require_safe
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok", "database": "ok"})


from sales.manager_override import manager_override

from sales import gift_views

urlpatterns = [
    path('pos/discount-reasons/', gift_views.discount_reasons, name='discount-reasons'),
    path('pos/gift-certificates/', gift_views.balances, name='gift-certificates'),
    path('pos/gift-certificates/lookup/', gift_views.lookup, name='gift-lookup'),path("", home, name="home"), path("customers/", customer_list, name="customers"), path("health/", health)]

@require_safe
def quick_find(request):
    destinations = {'customers': '/customers/', 'items': '/purchasing/items/',
                    'vendors': '/purchasing/vendors/', 'receipts': '/pos/history/',
                    'orders': '/pos/orders/', 'purchase-orders': '/purchasing/orders/'}
    destination = destinations.get(request.GET.get('area'), '/customers/')
    return redirect(destination + '?' + urlencode({'q': request.GET.get('q', '').strip()[:100],
                                                  'period': 'all', 'status': 'all'}))


urlpatterns += [
    path('customers/new/', customer_edit),
    path('customers/<int:pk>/edit/', customer_edit),
    path('customers/<int:pk>/charge-account/', charge_account),
    path('customers/<int:pk>/contact/', customer_contact),
    path('find/', quick_find),
    path('pos/sale/new/', sales.editor, {'kind': 'sale'}, name='new-sale'),
    path('pos/order/new/', sales.editor, {'kind': 'order'}, name='new-order'),
    path('pos/sale/<uuid:token>/', sales.editor, {'kind': 'sale'}, name='sale-detail'),
    path('pos/order/<uuid:token>/', sales.editor, {'kind': 'order'}, name='order-detail'),
    path('pos/history/', sales.document_list, {'section': 'history'}, name='sales-history'),
    path('pos/held/', sales.document_list, {'section': 'held'}, name='held-receipts'),
    path('pos/held/<uuid:token>/details/', sales.held_details),
    path('pos/held/<uuid:token>/delete/', sales.held_delete),
    path('pos/held/<uuid:token>/extend/', sales.held_extend),
    path('pos/orders/', sales.document_list, {'section': 'orders'}, name='order-list'),
    path('pos/awaiting-pickup/', sales.awaiting_pickup, name='awaiting-pickup'),
    path('pos/lookup/customers/', sales.lookup, {'kind': 'customers'}),
    path('pos/lookup/items/', sales.lookup, {'kind': 'items'}),
    path('pos/returns/lookup/', sales.return_lookup),
    path('pos/returns/<uuid:token>/lines/', sales.return_lines),
    path('pos/special-order-item/', item_views.special_order_item),
]

urlpatterns += [
    path('purchasing/items/name-check/', item_views.item_name_check),
    path('purchasing/items/<int:pk>/photo/', item_views.item_photo),
    path('purchasing/items/<int:pk>/tag/', item_views.item_tag),
    path('purchasing/items/<int:pk>/tag/print/', item_views.item_tag_print),
    path('purchasing/items/<int:pk>/details/', item_views.item_details),
    path('purchasing/po/new/', purchasing.editor, {'kind':'po'}),
    path('purchasing/voucher/new/', purchasing.editor, {'kind':'voucher'}),
    path('purchasing/po/<uuid:token>/', purchasing.editor, {'kind':'po'}),
    path('purchasing/voucher/<uuid:token>/', purchasing.editor, {'kind':'voucher'}),
    path('purchasing/lookup/items/', purchasing.item_lookup),
    path('purchasing/<str:kind>/<uuid:token>/pdf/', purchasing.document_pdf),
    path('purchasing/<str:kind>/<uuid:token>/print/', purchasing.document_print),
    path('purchasing/po/<uuid:token>/labels/pdf/', purchasing.po_labels_pdf),       # PO item labels
    path('purchasing/po/<uuid:token>/labels/print/', purchasing.po_labels_print),
    path('purchasing/labels/alignment/', purchasing.label_alignment_pdf),
]
# Items Trash: before the catalog patterns so 'purchasing/items/delete/' isn't read as an item pk page.
from purchasing import trash_views
urlpatterns += [path('purchasing/items/delete/', trash_views.delete_items, name='delete-items'),
                path('purchasing/trash/', trash_views.trash_list, name='items-trash'),
                path('purchasing/trash/restore/', trash_views.restore_items, name='restore-items')]
for section in ['items','vendors','departments']:
    urlpatterns += [path(f'purchasing/{section}/',purchasing.catalog,{'section':section}),path(f'purchasing/{section}/new/',purchasing.catalog_edit,{'section':section}),path(f'purchasing/{section}/<int:pk>/',purchasing.catalog_edit,{'section':section})]
for section in ['orders','history','held']:
    urlpatterns.append(path(f'purchasing/{section}/',purchasing.document_list,{'section':section}))

urlpatterns += [path('pos/sale/<uuid:token>/print-receipt/', sales.print_receipt), path('pos/sale/<uuid:token>/pickup/', sales.pickup_action), path('pos/order/<uuid:token>/payment/', sales.order_action, {'action':'payment'}), path('pos/order/<uuid:token>/fulfill/', sales.order_action, {'action':'fulfill'})]

from .records import remove_record
urlpatterns += [path('records/<str:section>/<int:pk>/delete/',remove_record)]

from accounts.cashiers import switch as cashier_switch
urlpatterns += [path('cashier/', cashier_switch)]
from accounts.views import TestingLoginView, TestingLogoutView
urlpatterns += [path('login/',TestingLoginView.as_view()),path('logout/',TestingLogoutView.as_view())]

urlpatterns += [path('printers/', printer_views.printer_list), path('printers/new/', printer_views.printer_edit), path('printers/<int:pk>/', printer_views.printer_edit), path('printers/<int:pk>/status/', printer_views.printer_status), path('printers/<int:pk>/test/', printer_views.printer_test), path('pos/order/<uuid:token>/print/', printer_views.print_sales_order)]

from purchasing.item_picker import item_picker
urlpatterns += [path('purchasing/item-picker/', item_picker)]

from accounts import admin_mode
from reports import views as reports
urlpatterns += [path('server-status/', admin_mode.server_status_view, name='server-status')]
from accounts import scanner_test  # admin-only Scanner Test page: QR size sheet + scan log, prints to the office Brother Tray 1
urlpatterns += [path('scanner-test/', scanner_test.scanner_test_view, name='scanner-test'),
                path('scanner-test/sheet.pdf', scanner_test.scanner_test_pdf), path('scanner-test/print/', scanner_test.scanner_test_print)]
urlpatterns += [path('reports/', reports.report_index, name='reports'), path('reports/sales-by-vendor/', reports.sales_by_vendor_view, name='report-sales-by-vendor'),
                path('reports/exchange/', reports.exchange_view, name='report-exchange'),
                path('reports/exchange/<str:day>.json', reports.exchange_download, name='report-exchange-download'),
                path('reports/exchange/<str:day>/re-export/', reports.exchange_reexport, name='report-exchange-reexport'),
                path('reports/sales-by-vendor/print/', reports.sales_by_vendor_print),
                path('reports/sales-tax/', reports.sales_tax_view, name='report-sales-tax'), path('reports/sales-tax/print/', reports.sales_tax_print),
                path('reports/daily-totals/', reports.daily_totals_view, name='report-daily-totals'), path('reports/daily-totals/print/', reports.daily_totals_print),
                path('reports/inventory-on-hand/', reports.inventory_on_hand_view, name='report-inventory-on-hand'), path('reports/inventory-on-hand/print/', reports.inventory_on_hand_print),
                path('reports/customer-balances/', reports.customer_balance_view, name='report-customer-balances'), path('reports/customer-balances/print/', reports.customer_balance_print),
                path('reports/general-sales/', reports.general_sales_view, name='report-general-sales'), path('reports/general-sales/print/', reports.general_sales_print)]

urlpatterns += [path('print/choice/', printer_views.print_choice), path('print/<str:source>/<uuid:token>/', printer_views.print_document), path('print/<str:source>/<uuid:token>/pdf/', printer_views.pdf_document)]

from tags import views as tags  # noqa: E402 Tag Generator
urlpatterns += [path('tags/', tags.index, name='tag-generator'), path('tags/furniture-sets/new/', tags.set_edit, name='new-furniture-set'),
                path('tags/furniture-sets/<uuid:token>/', tags.set_edit, name='furniture-set'), path('tags/furniture-sets/<uuid:token>/pdf/', tags.set_pdf),
                path('tags/furniture-sets/<uuid:token>/print/', tags.set_print), path('tags/furniture-sets/<uuid:token>/delete/', tags.set_delete),
                path('tags/item-tag/', tags.item_tag, name='item-tag'), path('tags/item-tag/preview.png', tags.item_tag_preview),
                path('tags/item-tag/print/', tags.item_tag_print)]  # Zebra Item Tag

from tracker import views as tracker  # noqa: E402 Notes & Requests (concept)
urlpatterns += [path('fixes/', tracker.index, name='fixes'), path('fixes/new/', tracker.new, name='new-fix'),
                path('fixes/<int:pk>/', tracker.detail, name='fix-detail'), path('fixes/attachments/<int:pk>/', tracker.attachment)]

from purchasing import reorder_views  # noqa: E402 Reorder Reminders
urlpatterns += [path('purchasing/reorder/', reorder_views.reminder_list, name='reorder-reminders'),
                path('purchasing/reorder/<int:pk>/ignore/', reorder_views.reminder_ignore)]

from purchasing import truck_views  # noqa: E402 Truck Receiving
urlpatterns += [path('purchasing/truck/', truck_views.truck_list, name='truck-list'),
                path('purchasing/truck/new/', truck_views.truck_new, name='new-truck'),
                path('purchasing/truck/new/scan/', truck_views.truck_scan),
                path('purchasing/truck/<uuid:token>/', truck_views.truck_detail, name='truck'),
                path('purchasing/truck/<uuid:token>/scan/', truck_views.truck_scan),
                path('purchasing/truck/<uuid:token>/state/', truck_views.truck_state),
                path('purchasing/truck/<uuid:token>/remove/', truck_views.truck_remove),
                path('purchasing/truck/<uuid:token>/receive/', truck_views.truck_receive),
                path('purchasing/truck/<uuid:token>/cancel/', truck_views.truck_cancel)]

urlpatterns += [path('pos/<str:kind>/<uuid:token>/manager-override/', manager_override)]
urlpatterns += [path('', include('shop_settings.urls'))]  # admin-only settings screens (Business, Tax)
urlpatterns += [path('settings/migrate-quickbooks/', include('sales.qbpos_urls'))]

from freedomshop import views as about  # noqa: E402 About FreedomShop
urlpatterns += [path('about/', about.about, name='about'), path('about/license/', about.license_text, name='about-license')]
