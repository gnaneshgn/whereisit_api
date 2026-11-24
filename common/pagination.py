from collections import OrderedDict

from django.conf import settings
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class StandardPagination(PageNumberPagination):
    """
    Page-number pagination producing the VizagMove envelope:

        {"success": true, <meta...>, "count", "page", "page_size",
         "total_pages", "next", "previous", "results"}

    Views can attach extra top-level metadata (e.g. last_synced_at,
    feed_status) by setting ``paginator.meta`` before building the response.
    """

    page_size_query_param = "page_size"

    @property
    def max_page_size(self):
        return getattr(settings, "API_MAX_PAGE_SIZE", 1000)

    def paginate_queryset(self, queryset, request, view=None):
        self.meta = getattr(self, "meta", None) or {}
        return super().paginate_queryset(queryset, request, view)

    def get_paginated_response(self, data):
        body = OrderedDict(success=True)
        body.update(self.meta)
        body.update(
            count=self.page.paginator.count,
            page=self.page.number,
            page_size=self.get_page_size(self.request),
            total_pages=self.page.paginator.num_pages,
            next=self.get_next_link(),
            previous=self.get_previous_link(),
            results=data,
        )
        return Response(body)
