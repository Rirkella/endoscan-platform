"""EndoScan model/dataset registry — the contract between dataset construction,
training, and serving (see ``docs/DATA_MODEL_AND_ARTIFACTS.md``).

This package provides:
- a typed schema for endpoint entries (`schema.py`),
- a thin JSON-backed read/write store (`store.py`),
- markdown/YAML card templates + a tiny renderer (`templates.py`).

No training, inference, data, or agent logic lives here.
"""

from .schema import EndpointEntry, EndpointIndex, EndpointStatus, ExplanationCapability
from .store import (
    EndpointNotFoundError,
    FrozenEndpointError,
    RegistryError,
    RegistryValidationError,
    find_repo_root,
    get_endpoint,
    list_endpoints,
    load_model,
    register_endpoint,
    register_or_update_endpoint,
    update_status,
)
from .templates import load_template, render_template, template_placeholders

__all__ = [
    "EndpointEntry",
    "EndpointIndex",
    "EndpointStatus",
    "ExplanationCapability",
    "EndpointNotFoundError",
    "FrozenEndpointError",
    "RegistryError",
    "RegistryValidationError",
    "find_repo_root",
    "get_endpoint",
    "list_endpoints",
    "load_model",
    "register_endpoint",
    "register_or_update_endpoint",
    "update_status",
    "load_template",
    "render_template",
    "template_placeholders",
]
