# EndoScan job runner

Containerized heavy-job entrypoint (`python -m endoscan_jobs run <job>`). Coverage
diagnostics now; the M5 build job later. Science lives in `endoscan_core`; this package
holds the CLI, runner, storage interface, and the impure source/identity adapters.
