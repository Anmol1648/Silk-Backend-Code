"""Cursor pagination → {items, nextCursor} (Doc 8 §4)."""
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response


class FundosCursorPagination(CursorPagination):
    ordering = "-created_at"
    page_size = 25
    cursor_query_param = "cursor"
    page_size_query_param = "limit"

    def get_paginated_response(self, data):
        return Response({"items": data, "nextCursor": self.get_next_link()})
