from django.urls import path

from apps.buses import views

app_name = "buses"

urlpatterns = [
    path("health/", views.HealthView.as_view(), name="health"),
    path("feed/status/", views.FeedStatusView.as_view(), name="feed-status"),
    path("buses/", views.BusListView.as_view(), name="bus-list"),
    # Fixed paths must come before the catch-all detail route.
    path("buses/live/", views.LiveBusListView.as_view(), name="bus-live"),
    path("buses/search/", views.BusSearchView.as_view(), name="bus-search"),
    path("buses/nearby/", views.NearbyBusesView.as_view(), name="bus-nearby"),
    path("buses/<str:vehicle_id>/", views.BusDetailView.as_view(), name="bus-detail"),
    # Live buses on a route: needs only the live feed.
    path("routes/<str:route_id>/buses/", views.RouteBusesView.as_view(), name="route-buses"),
    # Need GTFS static data (Phase 2). Explicit 501 until then.
    path("routes/", views.NotAvailableView.as_view(), name="route-list"),
    path("routes/search/", views.NotAvailableView.as_view(), name="route-search"),
    path("routes/<str:route_id>/", views.NotAvailableView.as_view(), name="route-detail"),
    path("routes/<str:route_id>/stops/", views.NotAvailableView.as_view(), name="route-stops"),
    path("stops/nearby/", views.NotAvailableView.as_view(), name="stop-nearby"),
]
