"""Errors raised by the notification service."""


class NotifyError(Exception):
    """Base class for notification errors."""


class TemplateError(NotifyError):
    """A template is missing or cannot be rendered."""


class GatewayTimeout(NotifyError):
    """The remote gateway did not answer in time. Retrying may succeed."""


class DeliveryError(NotifyError):
    """A notification could not be delivered. Carries the notification id."""

    def __init__(self, notification_id: str, reason: str = ""):
        super().__init__(f"{notification_id}: {reason}" if reason else notification_id)
        self.notification_id = notification_id
