"""EndoScan model/dataset registry — the contract between dataset construction,
training, and serving (PROJECT_RULES.md §2.4).

At M1 this package provides:
- a typed schema for endpoint entries (`schema.py`),
- a thin JSON-backed read/write store (`store.py`),
- markdown/YAML card templates + a tiny renderer (`templates.py`).

No training, inference, data, or agent logic lives here.
"""

from .schema import EndpointEntry, EndpointIndex, EndpointStatus
from .store import (
    EndpointNotFoundError,
    RegistryError,
    RegistryValidationError,
    find_repo_root,
    get_endpoint,
    list_endpoints,
    load_model,
    register_endpoint,
    update_status,
)
from .templates import load_template, render_template, template_placeholders

__all__ = [
    "EndpointEntry",
    "EndpointIndex",
    "EndpointStatus",
    "EndpointNotFoundError",
    "RegistryError",
    "RegistryValidationError",
    "find_repo_root",
    "get_endpoint",
    "list_endpoints",
    "load_model",
    "register_endpoint",
    "update_status",
    "load_template",
    "render_template",
    "template_placeholders",
]
