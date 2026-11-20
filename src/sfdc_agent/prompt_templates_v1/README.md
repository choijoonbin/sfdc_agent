# NOVA Prompt Markdown Authoring Standard · v1.0.0

This directory is the initial NOVA prompt baseline. There is no pre-v1 release lineage.

## Common document order

1. `# Objective` or one family-specific H1
2. `## Trust boundary` when source text is untrusted
3. task rules grouped under descriptive H2 or H3 headings
4. `## Output contract`
5. `## Untrusted source data` immediately before dynamic evidence

Keep instructions concise and imperative. Use bullets for independent rules and numbered lists only when order is part of the contract. Use inline code for identifiers, literals, event names, and short JSON shapes. Use fenced code only for fixed multiline code or JSON examples; never fence a placeholder or untrusted payload.

## Prompt families

### Tool acquisition

Applies to `screen.analyze`, `screen.capture`, `screen.discovery`, and `screen.metadata`.

- State the exact tool and call count.
- Put argument placeholders under a dedicated heading.
- End with the exact response contract, such as `OK` or one JSON object.

### Analysis and planning

Applies to local analysis, screen review and planning, and Story classification or analysis.

- Put the trust boundary before analysis rules.
- Keep schema examples compact and inline when possible.
- Separate observed, derived, and unresolved facts.
- Place source evidence last and label it untrusted.

### Generation

Applies to Screen and Story generation.

- Organize rules by source, component contract, layout, behavior, accessibility, verification, and output.
- Preserve pinned API names and response schemas byte-for-byte.
- Keep dynamic source and reviewed plans outside code fences.

### Repair

Applies to plan, Screen, and Story retry sections.

- Restate the frozen identity and output contract.
- Treat diagnostics and previous drafts as data.
- Put diagnostics after repair rules.

## Immutable template syntax

- `contract.json` is the publisher-facing manifest for section order and per-section placeholder multiplicity.
- Keep every `@@section` name and order unchanged.
- Keep every `{{placeholder}}` name, section, and multiplicity unchanged.
- Use LF line endings and balanced Markdown fences.
- A translated reference must preserve heading levels and order plus list markers, indentation, and order.
