"""HTTP routers.

Deliberately anaemic: parse the request, call a `whetstone.service` function, serialize the result.
Logic that shows up here belongs in `service.py`, where the CLI can reach it too.
"""
