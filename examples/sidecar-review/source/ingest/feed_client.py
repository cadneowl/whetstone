"""Supplier feed polling. No `.agents/` here either."""

import time

POLL_SECONDS = 60


class FeedClient:
    def __init__(self, http):
        self._http = http

    def fetch(self, supplier_id):
        return self._http.get(f"/feeds/{supplier_id}")

    def poll_forever(self, supplier_id, handle):
        while True:
            handle(self.fetch(supplier_id))
            time.sleep(POLL_SECONDS)
