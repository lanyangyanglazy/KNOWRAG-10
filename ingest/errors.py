from __future__ import annotations


class IngestError(Exception):
    code = "ingest_error"
    http_status = 500

    def __init__(self, message: str = "", **detail):
        super().__init__(message or self.code)
        self.detail = detail


class UnsupportedType(IngestError):
    code, http_status = "unsupported_type", 400


class TooLarge(IngestError):
    code, http_status = "too_large", 413


class DuplicateContent(IngestError):
    code, http_status = "duplicate_content", 409


class ReviewRequired(IngestError):
    code, http_status = "review_required", 422


class Quarantined(IngestError):
    code, http_status = "quarantined", 423


class QueueFull(IngestError):
    code, http_status = "queue_full", 429


class ModelUnavailable(IngestError):
    code, http_status = "model_unavailable", 503


class NotFound(IngestError):
    code, http_status = "not_found", 404


class Forbidden(IngestError):
    code, http_status = "forbidden", 403


class DocumentInactive(IngestError):
    code, http_status = "document_inactive", 409


class ParseError(Exception):
    """解析器内部失败；由 worker 转成 failed + parse_warning。"""
