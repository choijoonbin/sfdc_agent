# Design-system release consumption

SFDC_UI is the authored source for the common LWC core and conversion standards.
Agent executes approved conversion jobs; it does not read mutable Dev_Org common
sources or publish a new core identity. BACK selects an immutable design release
and derives an optional `designCore` request pin from the verified archive.

For a core release, Agent requires the raw `designManifestSha256` pin, verifies
the actual materialized `release.json`, marker entry, declared core inputs,
final LWC hashes and `release.source` identity before invoking Node or any
provider. Archived package files and all `nextbss` runtime/standard inputs must
match the committed input inventory in both directions, including nested
conversion-quality modules. Generated core metadata and the separately checked
virtual LWC output have explicit exceptions; extra runtime files are blocked.
If the request includes `designCore`, every field must match the
actual marker. The installed runtime must explicitly support the marker's
`compatibility.engineContract` in `managed-runtime-capabilities.json`.

Set `SFDC_MANAGED_ENGINE_ROOT` to the verified immutable runtime installed by
the server deployment process. A sibling UI checkout is useful for explicit
offline development tests, but cannot silently satisfy the runtime requirement
for new core jobs. Runtime archive identity, core source commit, raw authored
tree and final deployable LWC tree are distinct pins; none substitutes for
another. The runtime installer and existing server contract still verify their
own archive and tool pins.

The shared execution result path preserves the actual trusted marker in
`summary.designCore` for Screen/Story and ANALYZE/GENERATE results. A worker
cannot replace it. BACK verifies the same summary before admitting the result.
Generated screen artifact metadata can therefore record its core requirement
without copying editable common sources into an Agent workspace. Org screen
apply independently checks installed common module bytes and changes owned
screen metadata only.

Changed Figma inputs can return a `BLOCKED` result with a bounded
`coreChangeRequest` authoring proposal. Agent permits at most 2 MiB for the
proposal, checks gzip/base64, decoded JSON paths, byte counts and hashes, and
binds its parent core, release, target version and requested sources to this
build. The proposal remains blocked for reviewed source changes and a committed
CI rebuild; it cannot publish new LWC bytes under an old source identity.

Historical archives without `core-source.json` retain their historical release
behavior. They cannot satisfy a request that claims a new core marker. A marked
archive requires a manifest pin and compatible installed runtime even when a
caller omitted the optional wire marker.

On the `design` branch, CI defaults to the exact UI commit in
`deployment/design-runtime-lock.json`; the complete Agent gate checks out and
tests that commit. A manual override must still be an exact approved commit.
The lock provides deterministic verification tooling, not a mutable source
location or permission to deploy. Production uses the separately verified
runtime artifact and server deployment contract documented in
[`deployment/README.md`](../deployment/README.md).

`test_design_core.py` exercises artifact corruption, request substitution,
symlink boundaries, runtime compatibility and result identity. Managed HTTP
execution tests prove the guard runs before the worker and that provenance
reaches the actual response. These tests use synthetic artifacts and make no
Salesforce, Figma, model or provider calls.


V2 core markers preserve the full aggregate pin and add independent converter
runtime and Salesforce payload units. The Python verifier recomputes both the
archived runtime identity and the production module/package inventory of the
explicit `SFDC_MANAGED_ENGINE_ROOT` actually used for execution. An extra, missing
or different installed production module fails with `DESIGN_CORE_RUNTIME_MISMATCH`;
matching an engine compatibility version alone is insufficient. V1 markers retain
their strict original shape and historical behavior. Updating a converter unit
does not itself require redeploying identical Salesforce LWC; consumer CI must
still produce fresh typed same-payload retrieve/regression evidence for the new
aggregate before promotion.
