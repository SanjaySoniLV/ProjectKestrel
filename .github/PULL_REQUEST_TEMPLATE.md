<!--
Target `dev`, not `main` (main is for urgent fixes only). See AGENTS.md.
Keep each paragraph short. One purpose per PR.
-->

## The problem
<!-- What's broken, missing or awkward today, in plain terms. Assume the reader hasn't seen the issue before. -->

## How to reproduce
<!-- Steps or scenario that show the problem before this PR, so a reviewer can confirm it existed. -->

## What this PR does
<!-- The change, and why this approach over the obvious alternative. -->

## Checklist
- Kind: [ ] feature  [ ] fix  [ ] refactor (no behaviour change)  [ ] docs/tooling
- [ ] Tests: added or updated a test that runs the changed code (not a source-text check)
- [ ] `pytest analyzer/tests -m unit` passes locally
- [ ] Touches a contract surface? [ ] `.kestrel/` schema  [ ] settings keys  [ ] bridge path checks
      [ ] PyInstaller specs / packaging  [ ] bundled models or data (NOTICES.md updated)
- [ ] Deleted the code, comments and call sites this replaces (no stubs or tombstones)
- [ ] README / DEVELOPMENT.md updated if they describe what changed
- [ ] Nothing private: no internal infrastructure, private doc names, secrets or local paths

**Written by:** <!-- you / AI agent (which tool) -->   **Size:** <!-- +N / −N lines outside tests -->
