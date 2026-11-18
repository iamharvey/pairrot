"""Extension point for partner-specific renderers. Partners subclass Renderer in their own packages."""

from abc import ABC, abstractmethod

from demo.notify.models import Notification


class Renderer(ABC):
    @abstractmethod
    def render(self, n: Notification) -> str: ...


class PlainRenderer(Renderer):
    def render(self, n: Notification) -> str:
        return f"{n.subject}\n{n.body}"
