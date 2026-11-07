# SPDX-License-Identifier: AGPL-3.0-or-later
from django.urls import path

from . import backup_views, setup_views, views
from accounts import user_views

urlpatterns = [
    path('settings/users/', user_views.users, name='users-settings'),
    path('settings/users/logins/add/', user_views.login_change, {'action': 'add'}, name='users-login-add'),
    path('settings/users/logins/<int:pk>/<str:action>/', user_views.login_change, name='users-login-change'),
    path('settings/users/cashiers/add/', user_views.cashier_change, {'action': 'add'}, name='users-cashier-add'),
    path('settings/users/cashiers/<int:pk>/<str:action>/', user_views.cashier_change, name='users-cashier-change'),
    path('settings/backups/', backup_views.index, name='backups'),
    path('settings/backups/<str:stamp>/download/', backup_views.download, name='backup-download'),
    path('settings/backups/<str:stamp>/copy/', backup_views.restore_copy, name='backup-copy'),
    path('settings/backups/<str:stamp>/report/', backup_views.report, name='backup-report'),
    path('settings/backups/<str:stamp>/discard/', backup_views.discard_copy, name='backup-discard'),
    path('settings/backups/<str:stamp>/switch/', backup_views.switch, name='backup-switch'),

    path('settings/register/', views.register_rules_view, name='register-rules'),
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
