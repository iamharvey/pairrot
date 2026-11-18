"""Small helpers shared by the notification modules."""


def normalize_address(text: str) -> str:
    """Canonical form of an email address or phone number: trimmed and lower case."""
    return text.strip().lower()
