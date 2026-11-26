# Managed Agent server distribution

This distribution runs independently of the BACK local developer launcher,
personal HOME, `.venv`, SFDC_Ui source checkout and NOVA filesystem. It consumes
the matched immutable **UI Node runtime artifact**, not the mutable NOVA LWC
working tree. Existing local `sfdc-agent serve` behavior is unchanged.

## Artifact and image preparation

1. Build UI server artifacts from an approved exact UI commit using its
   `deployment/build-artifacts.mjs`. Keep `artifacts.json` with both archives.
2. Place only `artifacts.json`, `sfdc-node-runtime.tar.gz` and `vendor-tools.json`
   in `deployment/build-input/` (Git ignored). Use `vendor-tools.empty.json` for
   the core image. This directory is build input, never application source data.
3. Supply the independently approved UI commit, runtime archive SHA256 and
   `managed-runtime.json` byte SHA256 to the build. The installer checks all
   three pins, archive bounds/path safety, file inventory and every content hash
   **before** npm. It rejects links, special files, hidden files, `node_modules`,
   traversal and any attempt to merge over an installed release.

```sh
docker build -f deployment/Dockerfile \
  --build-arg UI_COMMIT=<approved-full-ui-commit> \
  --build-arg RUNTIME_ARCHIVE_SHA256=<approved-archive-sha256> \
  --build-arg RUNTIME_MANIFEST_SHA256=<approved-manifest-byte-sha256> \
  -t sfdc-agent-server:<release> .
python deployment/verify_image.py \
  --image sfdc-agent-server:<release> --output agent-server-proof.json
```

The image uses Python 3.11.15 and Node 26.13.0; Python production/build
dependencies are locked in `requirements-server.lock` plus the existing
`requirements.lock`. npm uses the imported `package-lock.json` with
`--omit=dev --ignore-scripts`. Installers never receive provider credentials.
The Dockerfile includes the Linux `libatomic1` dependency required by Node 26.
Fixed base-image version tags are recorded in `toolchain.json`; resolve and
approve the registry image digests before environment promotion.

The manually dispatched `server-artifacts.yml` workflow checks an approved UI
commit, runs policy tests, builds and checks the actual Linux image, and exports
it as an artifact. It does not create cloud infrastructure or deploy services.

## Optional Kiro and Chrome binaries

The core image deliberately has no unapproved downloaded Kiro/Chrome binary.
KIRO execution and visual capture return explicit unavailable responses unless
enabled with installed, pinned tools. `/health` success does not certify them.

`install_vendor_tools.py` accepts a public contract with an approved HTTPS host,
fixed version URL, archive SHA256, entrypoint SHA256 and explicit archive layout.
It supports regular-file-only tar.gz/tar.xz/zip, rejects links/traversal, installs
atomically and checks `--version` in a temporary HOME without credentials. It
never downloads a `latest` URL. See `vendor-tools.example.json`; its invalid
domain/zero pins are placeholders, not actual approved vendor packages.

For a tool-enabled image, replace `build-input/vendor-tools.json` with the
reviewed contract for that Linux architecture and rebuild. Capture official
vendor provenance and approved binary hashes during release review. A vendor
archive containing links must be repackaged into an approved regular-file-only
artifact, with the new archive hash recorded. The image records the successful
version/hash checks at `/opt/sfdc/vendor/vendor-tools.json`.

Chrome requires its Linux libraries/fonts, which this image installs. Its
existing Playwright `channel:'chrome'` is linked to the explicitly approved
installed Chrome entrypoint; Chromium is not substituted. Before enabling
`visualCapture`, perform a real nonroot browser launch/screenshot in the target
container sandbox. Kiro needs its installed binary and API-key-based headless
authentication; personal Kiro login caches are never copied.

## Server configuration

Use `agent-server-contract.example.json` and its JSON schema. Replace all
placeholder identifiers/pins and use the real shared S3 scope for the target
environment. The Python validator also enforces constraints JSON Schema cannot
express, such as matching paths, self-origin port and capability dependencies.

Start only through the managed entrypoint:

```sh
python -m sfdc_agent.server_runtime --contract /etc/sfdc/agent-server-contract.json --check
python -m sfdc_agent.server_runtime --contract /etc/sfdc/agent-server-contract.json
```

The contract contains public identifiers, names and hashes. Secret values are
injected by the deployment platform or mounted as private files. Set the
matching `_REF` variable to each declared secret reference. No secret value
belongs in contract JSON, Git, image build arguments, logs or image layers.

Required environment variables:

| Purpose | Variables |
| --- | --- |
| Server mode | `SFDC_AGENT_SERVER_MODE=1`, `SFDC_AGENT_MANAGED_ONLY=1` |
| Storage | `SFDC_STORAGE_PROVIDER=s3`, `AWS_REGION`, `SFDC_STORAGE_S3_BUCKET`, `SFDC_STORAGE_S3_PREFIX` |
| Immutable runtime | `SFDC_MANAGED_ENGINE_ROOT`, `SFDC_MANAGED_RUNTIME_SHA256`, `SFDC_MANAGED_EXECUTION_NODE` |
| Private state | `SFDC_AGENT_ROOT`, `TMPDIR` matching private app-owned directories, separate from the runtime |
| Worker/network | `SFDC_MANAGED_FIGMA_SCOPE=<tenant-uuid>/<workspace-uuid>`, `SFDC_MANAGED_AGENT_ORIGIN=http://127.0.0.1:<own-port>`, `SFDC_AGENT_TRUSTED_HOSTS` |
| Service auth | `SFDC_AGENT_EXECUTION_TOKEN` or `SFDC_AGENT_EXECUTION_TOKEN_FILE`, plus `SFDC_AGENT_EXECUTION_TOKEN_REF` |
| REST when enabled | `FIGMA_ACCESS_TOKEN` or `_FILE`, plus `FIGMA_ACCESS_TOKEN_REF`; optional official-reference token `FIGMA_PLAN_ACCESS_TOKEN`/`_FILE` plus `_REF` |
| Kiro when enabled | `KIRO_API_KEY` or `_FILE`, plus `KIRO_API_KEY_REF`, `NOVA_KIRO_CLI`, both `SFDC_AGENT_SCREEN_KIRO_MODE=headless` and `SFDC_AGENT_STORY_KIRO_MODE=headless` |
| Chrome when enabled | `SFDC_AGENT_CHROME_BIN` matching the pinned tool receipt |

Private secret files must be independent regular files, owned by root or the
service UID, with no group/other access (0400 or 0600), not symlinks. A secret
cannot be supplied by both environment and `_FILE`. Execution tokens must be
at least 32 printable ASCII characters. Provider values are never in reports.
IAM task/workload roles supply AWS credentials through the normal SDK chain;
developer AWS profiles/static access-key environment variables are rejected.

The service runs as UID/GID 10001. Runtime/config mounts are read only. Give
`/var/lib/sfdc-agent` and `/run/sfdc-agent` private writable scratch storage
with UID/GID 10001 and mode 0700. Scratch contents are disposable, not the source
of truth. Existing managed S3 materialization/publishing keeps sources, releases
and result artifacts in scoped shared storage. No host source checkout is
mounted into the container.

## Routing and readiness

One process currently has one REST Figma credential scope. Route each tenant /
workspace to its dedicated worker deployment. The boundary rejects a different
scope before work and never swaps credentials or changes a selected engine.
`REST_ONLY` remains REST-only; `LOCAL_FILE` remains an explicitly selected .fig
archive. An omitted remote Kiro Screen acquisition defaults to MCP in the
existing engine and therefore returns `FIGMA_MCP_SERVER_NOT_READY` on this
server; it is never silently converted to REST.

Run **one Uvicorn worker** per container: the Node child calls this same process
on loopback with an in-memory job/prompt context. Scale containers with the
same scoped routing contract, rather than increasing Uvicorn workers or
changing self-origin to a load-balanced address. Spring calls Agent through
private DNS/service authentication. External ingress must not expose internal
Agent APIs directly; untrusted forwarded headers are disabled.

`/health` is local liveness after startup checks. The service-token-protected
`/internal/v1/server-readiness` states that external providers were **not
contacted**, and `deploymentReady:false`. Local packaging success does not
prove S3 IAM, external provider consent/authentication or operational topology.
`verify_image.py` verifies actual Linux startup, nonroot/read-only operation,
private service auth, existing request validation, cross-scope denial and
disabled provider boundaries using synthetic data; it stops its own container.

## Figma MCP server OAuth remains a separate implementation

The current MCP first consent is a local CLI/loopback browser flow with local
credential HOME/lock files. Copying that HOME into a container does not provide
server user consent or distributed credential management. This distribution
always gates MCP as `NOT_READY`, including its collect endpoint, and cannot be
enabled by a JSON flag. REST and selected local .fig flows remain available.

`figma-mcp-server-promotion.json` is the machine-checked future contract, not a
claim of implementation. Before enabling MCP, implement and prove HTTPS
callback + state/PKCE/nonce, tenant/workspace/user-bound consent, encrypted shared
credentials, refresh/key rotation, revocation on disconnect/user removal,
distributed locking, two-node operation, restart recovery and no credential
logging. Promotion needs an implementation release plus recorded passing
evidence; no fallback to another source or engine is permitted on MCP failure.

## Acceptance after infrastructure is provisioned

1. Confirm private service auth, exact UI/runtime pins and matching BACK/Agent
   S3 bucket/prefix/region and tenant/workspace UUIDs.
2. Exercise source/release download and result publication using workload IAM,
   including cross-scope denial, cancellation, restart and replica replacement.
3. Authenticate scoped Figma REST and optional Kiro API-key CLI without developer
   files; run one conversion against the approved immutable release.
4. For Chrome-enabled images, prove fonts, screenshot fidelity and nonroot
   sandbox operation. Enable MCP only after its separate promotion evidence.

Salesforce service-account JWT setup and isolated CLI HOME belong to BACK's
server preparation, not this Agent image.
