"""Push delivery. Best effort: a caller is never blocked on a notification."""


class TransportError(Exception):
    pass


class NotificationService:
    def __init__(self, transport):
        self._transport = transport
        self._dropped = 0

    def send(self, user_id, template, payload):
        self._transport.push(user_id, template, payload)
