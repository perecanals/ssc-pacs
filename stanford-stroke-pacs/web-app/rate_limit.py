"""Shared slowapi rate limiter.

Endpoints decorate themselves with ``@limiter.limit(...)``; app.py exposes the
limiter on ``app.state.limiter`` and installs the 429 handler (both required
by slowapi at request time).
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
