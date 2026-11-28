from django.urls import path

from apps.buses import views as bus_views
from apps.transit import views

app_name = "transit"

urlpatterns = [
    # Routes (fixed paths before the catch-all detail route)
    path("routes/", views.RouteListView.as_view(), name="route-list"),
    path("routes/search/", views.RouteSearchView.as_view(), name="route-search"),
    path("routes/<str:route_id>/", views.RouteDetailView.as_view(), name="route-detail"),
    path("routes/<str:route_id>/stops/", views.RouteStopsView.as_view(), name="route-stops"),
    # Live feed only: works with or without GTFS
    path("routes/<str:route_id>/buses/", bus_views.RouteBusesView.as_view(), name="route-buses"),
    # Stops
    path("stops/nearby/", views.NearbyStopsView.as_view(), name="stop-nearby"),
    path("stops/search/", views.StopSearchView.as_view(), name="stop-search"),
    path("stops/buses/", views.StopBusesView.as_view(), name="stop-buses"),
    path("stops/<str:stop_id>/", views.StopDetailView.as_view(), name="stop-detail"),
    path("stops/<str:stop_id>/arrivals/", views.StopArrivalsView.as_view(), name="stop-arrivals"),
    # Trips, journeys, live bus timeline
    path("trips/<str:trip_id>/", views.TripDetailView.as_view(), name="trip-detail"),
    path("journeys/", views.JourneyView.as_view(), name="journeys"),
    path("buses/<str:vehicle_id>/trip/", views.BusTripView.as_view(), name="bus-trip"),
]
