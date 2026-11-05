# SPDX-License-Identifier: AGPL-3.0-or-later
from django.urls import path

from . import qbpos_views as views

urlpatterns = [
    path('', views.upload, name='qbpos-upload'),
    path('imports/', views.imports, name='qbpos-imports'),
    path('<uuid:import_id>/', views.preview, name='qbpos-preview'),
    path('<uuid:import_id>/undo/', views.undo, name='qbpos-undo'),
]
