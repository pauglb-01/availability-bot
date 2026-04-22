"""WhatsApp outbound dispatcher placeholder.

TODO: Implement WhatsApp outbound dispatcher.
"""


class WhatsAppOutboundDispatcher:
    """Placeholder for WhatsAppOutboundDispatcher."""

    def __init__(self, logger_name: str = __name__):
        self.logger_name = logger_name

    def send_text_message(self, phone: str, text: str) -> None:
        """Send a text message via WhatsApp."""
        raise NotImplementedError("WhatsAppOutboundDispatcher not implemented yet")
