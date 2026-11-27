# Agent unit runtime dependency

The Agent's complete unit gate includes the real Node projection/exporter shared with the UI. An Agent-only checkout is insufficient; missing runtime files or skipped tests fail the required gate.

CI checks out `choijoonbin/sfdc_ui` as the sibling `SFDC_Ui`, installs its locked npm dependencies, and uses Node 26.13.0. Configure:

- Repository variable `SFDC_UI_RUNTIME_REF`: the approved, immutable 40-character UI commit SHA for existing development branches. The `design` branch defaults to the reviewed `deployment/design-runtime-lock.json` commit.
- Secret `SFDC_RUNTIME_READ_SSH_KEY`: a read-only deploy key scoped to that private UI repository.

A manual workflow may supply `ui_runtime_ref` with another exact approved commit. There is no branch/latest fallback. New core jobs also require the explicit installed engine and archive checks described in [the design-system consumer contract](../docs/design-system-consumer.md). CI configuration is separate from local test evidence; no successful GitHub run is claimed until these dependencies are configured and the workflow actually completes.

Local matching layout:

```text
workspace/SFDC_Agent
workspace/SFDC_Ui
```

After installing `requirements.lock` and the sibling `package-lock.json`, run:

```sh
python scripts/verify-unit-contracts.py --ui-root ../SFDC_Ui --report /private/evidence/agent-proof.json
```

The preflight verifies the runtime files, imports the actual shared Node modules, validates all 72 NOVA module sources, and records their hashes. The unit runner removes inherited provider credentials and rejects skipped tests or source changes. It does not provide model or provider acceptance evidence.
