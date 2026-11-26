# Immutable model-free NOVA library fixture

These are fixed test artifacts adapted once from the approved
`SFDC_BACK/src/test/resources/model-free-nova-g2` fixture. The verifier has no
runtime dependency on that repository, a developer NOVA checkout or Dev Org.
They are not a second editable component source or an application release.

`library-files.zip` contains the 72 NOVA public library modules and their
descriptor/API/module manifests (242 regular files, 728,075 expanded bytes).
`component-library.json` is the immutable tuple consumed by the actual Node
`readComponentLibrary` reader. It verifies each declared module file hash, the
dependency graph and the public contract using the explicitly pinned UI code.

| Artifact | Bytes | SHA256 |
| --- | ---: | --- |
| library-files.zip | 220448 | `b20a4ffadbab1b8ed1fc706c83af8a02a0c90859977c5b9e7152763b447464fa` |
| component-library.json | 411 | `486cb0a2e96c98afb1b2bd44e7ff5d846118bf0315e5c88b5c567a42a8062381` |

The source archive before adaptation was 224,015 bytes with SHA256
`63ea348de536ec72bd8d0551e8b36ae878450e7f902e339cb42451193afd206c`;
its original component tuple SHA256 was
`ce2728a3cc8addc90af404d632427886c03a4415725a2428cf636fbb5e9f01b2`.
The approved UI commit `d4282424fa91e31f7142e56dfda6d85c6ced7b6d` derived
the public property metadata from those exact same 72 fixed module sources.
Only `nextbss/design-system/nova-public-contract.json` was replaced inside the
archive (its SHA256 is now
`40e5a29aef8a006340c3706f4084d222d3b05eb5e8f64456118c27bc92269dd9`),
and only `contractSha256` changed in `component-library.json`. An entry-by-entry
byte comparison against the original archive verified that every other file,
including all LWC JS/CSS/HTML/XML, descriptor and module manifest, is unchanged.
The ZIP container was repacked deterministically; compressed ZIP bytes are not
module-source bytes.

The standard unit verifier pins these identities in code. Fixture updates need
an explicit reviewed pin change. It never restores LWC source into the UI
working tree: a bounded temporary read-only byte map supplies the fixture to
the selected Node runtime. No Figma, Salesforce or model request is made.
No public contract is regenerated during a verification run: incompatible
pins, changed fixture bytes and future derived-API drift remain failures.
