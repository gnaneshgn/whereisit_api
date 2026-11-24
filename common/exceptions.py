"""
Consistent error envelope for every API failure:

    {"success": false, "error": {"code": "...", "message": "...", "details": {...}}}
"""
import logging

from django.http import JsonResponse
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


class ServiceUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Live data is temporarily unavailable."
    default_code = "service_unavailable"


def _error_body(code, message, details=None):
    error = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {"success": False, "error": error}


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        # Unhandled exception: log it, never leak internals to the client.
        logger.exception("Unhandled API error", exc_info=exc)
        return JsonResponse(
            _error_body("server_error", "An unexpected error occurred."),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if isinstance(exc, ValidationError):
        response.data = _error_body("invalid_input", "Invalid request parameters.", response.data)
    else:
        detail = response.data.get("detail", response.data) if isinstance(response.data, dict) else response.data
        code = getattr(detail, "code", None) or getattr(exc, "default_code", "error")
        response.data = _error_body(str(code), str(detail))
    return response


def json_404(request, exception=None):
    return JsonResponse(_error_body("not_found", "The requested resource was not found."), status=404)


def json_500(request):
    return JsonResponse(_error_body("server_error", "An unexpected error occurred."), status=500)
