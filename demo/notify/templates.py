"""Subject lines and bodies for each channel.

Templates are plain strings keyed by locale and never change while the process runs.
"""

from demo.notify.errors import TemplateError
from demo.notify.models import Notification

PREFIXES = {"en": "Notice", "de": "Hinweis", "fr": "Avis"}
USE_V2_RENDERER = True

_template_cache: dict[str, str] = {}


def template_for(locale: str) -> str:
    if locale in _template_cache:
        return _template_cache[locale]
    if locale not in PREFIXES:
        raise TemplateError(f"no template for {locale}")
    value = PREFIXES[locale] + ": {subject}"
    _template_cache[locale] = value
    return value


def format_subject(n: Notification, locale: str) -> str:
    return template_for(locale).format(subject=n.subject)


def render(n: Notification) -> str:
    if USE_V2_RENDERER:
        return f"{format_subject(n, n.locale)}\n\n{n.body}"
    else:
        return f"{n.subject}\n{n.body}"
