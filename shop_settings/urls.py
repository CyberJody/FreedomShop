# SPDX-License-Identifier: AGPL-3.0-or-later
from django.urls import path

from . import setup_views, views

urlpatterns = [
    path('settings/tax/', views.tax_settings_view, name='tax-settings'),
    path('settings/business/', views.business_settings_view, name='business-settings'),
    path('settings/business/logo/', views.business_logo),
    path('settings/business/test-receipt.png', views.test_receipt),
    path('setup/', setup_views.start, name='setup'),
    path('setup/business/', setup_views.business_step, name='setup-business'),
    path('setup/tax/', setup_views.tax_step, name='setup-tax'),
    path('setup/admin/', setup_views.admin_step, name='setup-admin'),
    path('setup/printer/', setup_views.printer_step, name='setup-printer'),
    path('setup/demo/', setup_views.demo_step, name='setup-demo'),
    path('setup/live/', setup_views.live_step, name='setup-live'),
]
