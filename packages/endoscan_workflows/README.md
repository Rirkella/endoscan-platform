# endoscan-workflows

EndoScan's durable workflow-orchestration and agent-integration package. It coordinates typed agent proposals, deterministic scientific operations, immutable evidence, version-bound approvals, and endpoint lifecycle state.

## Agentic pipeline map

- [`service.py`](endoscan_workflows/service.py) — workflow orchestrator and control plane.
- [`harness.py`](endoscan_workflows/harness.py) — bounded agent runtime, budgets, traces, tool execution, and persistence.
- [`discovery.py`](endoscan_workflows/discovery.py) — dataset discovery and evaluation agent role.
- [`training_dataset.py`](endoscan_workflows/training_dataset.py) — endpoint specification, dataset review, and agent contracts.
- [`endpoint_lifecycle.py`](endoscan_workflows/endpoint_lifecycle.py) — strategy-agent boundary and lifecycle artifacts.
- [`openai_provider.py`](endoscan_workflows/openai_provider.py) — OpenAI Agents SDK adapter.
- [`providers.py`](endoscan_workflows/providers.py) — provider-neutral runtime registry and deterministic offline provider.
- [`tools.py`](endoscan_workflows/tools.py) — reviewed tool registry.
- [`semantics_v2_executor.py`](endoscan_workflows/semantics_v2_executor.py) — deterministic provider-task execution.
- [`state_machine.py`](endoscan_workflows/state_machine.py) and [`workflow-state-machine.json`](endoscan_workflows/workflow-state-machine.json) — enforced transitions.
- [`database.py`](endoscan_workflows/database.py), [`repository.py`](endoscan_workflows/repository.py), and [`artifacts.py`](endoscan_workflows/artifacts.py) — durable state and immutable evidence.

## Responsibility boundary

Agents propose typed plans and strategies. Reviewed deterministic tools retrieve, normalize, calculate, assemble, and train. Human reviewers authorize scientific definitions, source access, aggregation, datasets, training, validation, and publication. An agent cannot directly publish a model, execute arbitrary URLs, or bypass a version-bound approval.

## Offline and live execution

Tests and demonstrations use `DeterministicOfflineProvider`, a fixture-backed offline model-provider implementation behind the same provider-neutral agent interface. The OpenAI Agents SDK adapter is implemented, but live use requires explicit configuration, credentials, workflow authorization, and bounded budgets. Offline validation establishes software behavior; it is not live-source or scientific validation.

## Verification

Package tests cover the [agent harness](tests/test_harness.py), [OpenAI adapter](tests/test_openai_provider.py), persistence, strict contracts, tools, providers, and lifecycle transitions. Repository-level tests exercise API and cross-package behavior. The [offline demo guide](../../docs/OFFLINE_DEMO.md) documents two zero-network complete lifecycle demonstrations.
