# Conversion evidence and provider boundaries

The managed prompt registry remains the only versioned prompt authority. Do not add a second set of conversion rules in Kiro steering files.

## Responsibilities

- The shared `source-evidence.mjs` engine module normalizes source snapshots, preserves authored facts and records source identity, revision provenance and unsupported coverage. REST and local archives use this contract.
- `kiro-screen-context.mjs` carries measured layout, complete rich-text override tables/runs, variable bindings and vector evidence into the bounded Screen context. An incomplete run table stays unresolved; it is never reconstructed from a screenshot guess.
- `kiro-nova-contract.mjs` projects derived mappings and regenerates their guidance from the pinned NOVA public API. Legacy renderer metadata is not model guidance. Authored Figma text remains untrusted evidence without rewriting.
- `kiro_screen_projection.mjs` applies that same projection to saved analysis checkpoints. `nova_prompt_evidence.py` validates the final derived input rather than maintaining a second mapping implementation.
- `provider_capabilities.py` declares implemented input/output capabilities and creates bounded reproducibility receipts. `kiro_gateway.py` transports tasks; it does not choose an automatic replacement model.

## Provider receipts

Prompt responses and provider evaluations include `providerReceipt` with the configured model, explicitly reported model, capability flags, and model-selection policy. Kiro includes the launched CLI byte hash and executable basename. No local executable path or credentials are included. If the CLI does not report a version/model, the receipt records an unreported value rather than guessing.

All current conversion adapters submit structured text. Image input remains disabled: Kiro reports it unsupported; OpenAI/Azure report it not enabled by these adapters. Enabling multimodal input requires an explicit adapter, model capability check, immutable image/reference binding and tests; provider branding alone is not evidence of image support.

These receipts improve traceability. They do not guarantee deterministic LLM output or establish visual accuracy; the common browser oracle and fixed-state fixtures measure the generated result separately.
