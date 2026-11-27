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
    # Route, stop, trip and journey endpoints live in apps.transit.urls
    # (they answer 501 not_available until GTFS is imported).
]
