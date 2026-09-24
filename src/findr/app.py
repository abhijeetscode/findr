"""ASGI entrypoint. The real composition root lives in
adapters/inbound/http/app.py; this re-export just keeps
`findr.app:app` / `fastapi dev src/findr/app.py` working as the run target.
"""

from findr.adapters.inbound.http.app import app

__all__ = ["app"]
