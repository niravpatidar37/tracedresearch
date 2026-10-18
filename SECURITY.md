# Security policy

## Reporting a vulnerability

Please report vulnerabilities **privately** through GitHub:
[Security → Report a vulnerability](https://github.com/niravpatidar37/tracedresearch/security/advisories/new).
Do not open a public issue for a security problem.

Useful reports include: a way past the SSRF guard (reaching a private, loopback, link-local or metadata
address), a prompt-injection path that makes a model trigger an action or plants a link in the rendered
report, a citation that passes the validator without a matching Finding record, a budget that can be
exceeded, or one API key reading another key's runs.

You should get an acknowledgement within 7 days. Fixes ship on `main`; there are no release branches.

## Scope

- In scope: the code in this repository (`src/`, the Dockerfile, the workflows, the Terraform).
- Out of scope: third-party sites returned by search, and the LLM providers themselves.
- Only test against your own local instance. Never point the service at systems you do not own.

The threat model, controls and residual risks are in the README's [Security](README.md#security-model) section.
