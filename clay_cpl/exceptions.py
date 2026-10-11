"""Clay CPL CLI exceptions."""


class ClayError(Exception):
    """Base exception for Clay CPL CLI."""


class ClayAuthError(ClayError):
    """Authentication failed — bad credentials or expired session."""


class ClayAPIError(ClayError):
    """API returned an error response."""

    def __init__(self, message, status_code=None, response_body=None, network=False):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.network = network  # connection refused, reset or timed out - no HTTP status

    @property
    def should_retry(self):
        return self.status_code is not None and self.status_code >= 500


class ClayTimeoutError(ClayError):
    """Operation timed out (e.g., polling for table completion)."""
