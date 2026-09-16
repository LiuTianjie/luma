class LumaError(Exception):
    """Base exception for expected CLI failures."""


class ControlRequestError(LumaError):
    """A transient transport failure, not an application or authentication error."""
