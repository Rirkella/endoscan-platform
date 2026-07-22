# Endpoint reproduction pipelines

This directory contains deterministic configuration, runners, and staging transforms used
to reproduce the existing ER and AR endpoint assets. It is not the application workflow
engine: durable lifecycle state, providers, agent execution, artifacts, approvals, assembly,
training coordination, and publication belong to `packages/endoscan_workflows`.

`endpoints/ER/run.py` is the reusable config-driven training runner. The endpoint directories
contain reviewed configurations; ER also contains source-specific staging transforms. These
paths remain because current registry and workflow inspection contracts reference them and
because they document how the committed serving artifacts can be regenerated.

Real reproduction is not part of setup, CI, or the offline demo. It requires separately
authorized source material, a reviewed local configuration, explicit human approval, and an
independent artifact/metric review before registry publication. No script in this directory
should be interpreted as a second workflow orchestrator or an automatic live-data command.

The committed `config.real.yaml` remains approval-blocked. Copy an example to an untracked
local configuration for an authorized reproduction; never commit credentials, source
downloads, local DVC paths, or an approval-bearing working configuration.
