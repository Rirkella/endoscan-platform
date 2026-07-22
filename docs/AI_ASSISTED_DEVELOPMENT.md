# AI-assisted development

EndoScan has used AI tools during architecture exploration, implementation, review, and
test generation. AI assistance is part of the engineering process, but it is not an
authority for scientific truth, security decisions, or release approval.

## Engineering boundary

AI tools may propose designs, draft code and documentation, identify risks, and generate
deterministic tests. Every retained change is reviewed against the repository's typed
contracts, offline test suites, provenance rules, security controls, and human approval
boundaries. Generated text is not accepted as evidence that a provider works live or that
an endpoint is scientifically valid.

Private prompts, chain-of-thought, temporary task instructions, credentials, local paths,
and execution transcripts are not product artifacts and are not kept in the public source
tree. Durable engineering rules belong in [Contributing](CONTRIBUTING.md), while current
architecture and status belong in their canonical documents.

## Product agents are different

The endpoint-building agents described in [Architecture](ARCHITECTURE.md) are product
components. They run through provider-neutral interfaces, bounded typed tools, durable
workflow state, immutable artifacts, budgets, and explicit approvals. Their proposals are
validated by deterministic code and do not directly assemble, train, approve, or publish a
scientific endpoint.

AI-assisted software development does not bypass that product boundary. A human remains
responsible for scientific policy, approval decisions, security-sensitive operations, and
claims made about model or dataset validity.
