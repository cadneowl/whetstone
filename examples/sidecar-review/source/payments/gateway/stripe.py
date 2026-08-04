"""The card processor hop. Every external call in `payments/` goes through here."""

import time

MAX_RETRIES = 3
BACKOFF_SECONDS = 0.5


class StripeGateway:
    def __init__(self, http):
        self._http = http

    def authorize(self, token, amount_cents):
        last = None
        for attempt in range(MAX_RETRIES):
            try:
                return self._http.post(
                    "/v1/charges", json={"token": token, "amount": amount_cents}
                )
            except TimeoutError as exc:
                last = exc
                time.sleep(BACKOFF_SECONDS * (2**attempt))
        raise last
