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

## Live validation on 2026-11-25

The [recorded benchmark](validation/2026-11-25-live-conversion.json) covers one Screen (`29876:14784`) through the normal Main APIs. Kiro analysis, code generation and repair made real model requests. Their recorded request path took 434.32 seconds of 453.17 seconds of processing stages, excluding 66.79 seconds of manual review. These intervals include bridge/CLI overhead; they do not measure model computation or first-token latency independently. NOVA archive stages took 17.86 seconds; each path has only one completed sample.

With both comparison canvases explicitly fixed to white, the exact-revision REST NOVA capture differed from the official Figma 1× PNG by 0.9954% at RGB tolerance 12. The ordinary Main capture measured 1.06%, but its deliberate Lightning `#F3F3F3` canvas differed from the white canvas used to composite the transparent source. That context mismatch is not evidence of an authored-color conversion error. Fixing the isolated capture document's canvas changed no LWC CSS or saved artifact. The same archive input produced reference-only ratios of 0.85% for NOVA and 2.53% for Kiro. This archive is not linked to the current remote revision, and the earlier 1.13%/1.30% report-plus-capture pairs were unavailable. No accuracy improvement rate is established.

Browser probes found that a low visual difference does not ensure operable controls: REST NOVA lacked a native service-number input and reset button; archive NOVA lacked a native search button. Kiro input, reset and required-field validation worked. Actual business API behavior remains unverified. Org apply was rejected with `ORG_REVIEW_REQUIRED`; no Salesforce metadata was deployed. Further pinned-source runs were blocked by an observed Figma nodes API 429. The managed error path currently conceals the underlying code, so that observation does not establish the cause of every earlier failed job.

The same archive also exposed a digest-contract limitation: NOVA adds `draftSize` on 78 nodes and `draftAsset` on three vectors before computing `sceneSha256`; Kiro does not. Removing only those derived fields makes the snapshots identical. Preserve existing v1 receipts; a future contract must separate authored scene evidence from generated rendering assets. Reducing prompts to stage-specific evidence and the required public API closure should be tested after these correctness gaps are addressed. Full raw source evidence must remain intact.

After validation, the user requested complete cleanup of test data. The ten test jobs, three saved analyses, three results, linked preview/comparison records and three prompt-input traces were removed. Thirty-eight test-exclusive objects and both temporary capture/session directories were deleted. All ten job and three result URLs returned 404. Original Figma sources, shared assets, active releases and the existing Org catalog were unchanged; no test deployment had reached the Org. Only sanitized measurement documentation and normal audit history remain.

## Common remediation on 2026-11-26

New source receipts use schema v2. `authoredSceneSha256` (also exposed as `sceneSha256`) excludes only the adapter's `draftSize`/`draftAsset`; derived rendering assets have their own digest. `rawSnapshotSha256` fingerprints the complete captured input object, not the original HTTP response or `.fig` file bytes. `provenanceSha256` remains independent. Existing v1 receipts keep their original algorithm; a v2 receipt never reinterprets an old hash. A valid archive hash does not prove a remote revision relationship.

The shared browser oracle checks source-identified native inputs, buttons, required/disabled/read-only states, local reset/search events, and authored empty-table state. It captures the initial screenshot before interaction probes. Missing or ambiguous host evidence stays unverified; confirmed absent/broken native controls block technical eligibility. An unresolved business API is recorded independently, without invented options, data rows or API success. Visual repair must preserve these checks even if its pixel score improves.

Comparison receipts fix both canvases to white and record the actual browser, font hash/load state, viewport and DPR. Export scale, bounds and revision are populated only from captured reference metadata; absent values stay unverified. These are test-document settings and do not change generated LWC styles.

The canonical `execution-diagnostics-policy.json` lives in SFDC_UI and is packaged byte-for-byte in Python and Spring. A nonzero worker may return only the bounded, versioned allowlisted code/stage packet. Provider messages, source text, credentials, local paths and stderr remain private. Spring persists validated diagnostics on the fenced failed attempt; SFDC_UI uses the same error interpretation for an active observer and recovered jobs. Model/quota failures do not automatically trigger another billable call.

`provider_observation.py` records client-side first stream byte, first assistant text arrival, authentication, model-process, parsing and cleanup intervals. These are not provider-internal inference time or network TTFT. Token counts are recorded only when explicitly returned by the CLI; missing counts remain `null`/`unreported`.

`prompt_projection.py` preserves complete source evidence and saved review checkpoints. It deduplicates an evidence-coverage alias only when canonical typed JSON is equal. Screen analysis retains full API guidance. Generation/repair retain every public module identity and API signature; full prose is retained for verified used components and utility/style modules, while unused component prose is deferred. Unknown component selections fall back to the full contract. The bounded cache keys the entire pinned contract and selected component set. Story input serialization remains unchanged. No prompt template or screen-specific rule was added by this optimization.

The [November 26 measurements](validation/2026-11-26-common-remediation.json) confirm equal authored-scene and provenance digests for actual NOVA/Kiro runs on the same archive, with intentionally separate derived-asset hashes. NOVA and Kiro each passed native-control coverage 7/7; the final NOVA source metric coverage was 27/27. A real Sandbox LWC application, owned test tab, browser rendering, input/reset and required-empty checks succeeded. Unpublish/remove succeeded and restored the original page's canonical digest. All active test results, ten jobs, saved analyses and prompt traces were removed. One archived result and its referenced objects remain because the existing immutable Org-version audit requires them; no immutability guard was disabled.

The matching-revision remote comparison remained blocked by Figma quota. Archive comparisons remain reference-only: NOVA search/table differences changed from 0.8453%/2.7405% to 0.9023%/2.7546%; the observed Kiro difference was 2.5254%. Native lookup controls improved, while visual accuracy improvement was not established. The popup INSTANCE remains outside the Screen FRAME/GROUP root contract. The real Kiro processing stages took 220.39 seconds excluding manual review; 193.72 seconds belonged to model-request stages. These single samples do not establish a general speed improvement. First-content counters start at the model-process stream after authentication and are not request TTFT.
