from django.contrib import admin
from django.urls import include, path

from common.exceptions import json_404, json_500

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("apps.buses.urls")),
    path("api/", include("apps.transit.urls")),
]

handler404 = json_404
handler500 = json_500
