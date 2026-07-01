"""EndoScan thin serving API — a READ-ONLY FastAPI over ``endoscan_core``.

The service holds NO science: every route is a thin wrapper over a tested
``endoscan_core`` function (``registry.store`` for listing/lookup, ``inference`` for
predict/explain/limitations). Models are the COMMITTED ``model.pkl`` artifacts loaded
directly from git (no DVC pull, no credentials). See ``app.create_app``.
"""

from __future__ import annotations

from .app import create_app

__all__ = ["__version__", "create_app"]

__version__ = "0.0.0"
