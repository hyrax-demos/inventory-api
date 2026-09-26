"""Domain errors raised by service-layer code.

Services raise these instead of ``HTTPException`` so the business logic
does not depend on FastAPI. Each error has a stable machine-readable
``code``, a human ``message`` and the HTTP ``status_code`` the web layer
should use. The status codes follow what the existing routes already
return: 400 for bad input, 404 for missing or other-tenant resources, and
409 for conflicts.
"""


class DomainError(Exception):
    status_code = 500

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message or code
        super().__init__(self.message)


class ValidationError(DomainError):
    """The request is malformed or breaks an input rule."""

    status_code = 400


class NotFoundError(DomainError):
    """A referenced resource does not exist within the caller's tenant."""

    status_code = 404


class ConflictError(DomainError):
    """The request is well-formed but conflicts with the current state."""

    status_code = 409
