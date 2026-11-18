"""Client for the remote delivery gateway. post() goes over the network and can raise GatewayTimeout."""

from demo.notify.errors import GatewayTimeout


class Gateway:
    def __init__(self, transport):
        self.transport = transport

    def post(self, channel: str, recipient: str, body: str) -> str:
        """Send one message and return the gateway's message id."""
        reply = self.transport(channel, recipient, body)
        if reply is None:
            raise GatewayTimeout(f"{channel} to {recipient}")
        return reply
