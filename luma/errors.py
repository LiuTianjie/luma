class LumaError(Exception):
    """Base exception for expected CLI failures."""


class ControlRequestError(LumaError):
    """A transient transport failure, not an application or authentication error."""


class DatabaseBusyError(LumaError):
    """Control cannot acquire its database writer within the request budget."""


class NomadUpgradePending(Exception):
    """A saved migration step is still running and should be polled again."""
