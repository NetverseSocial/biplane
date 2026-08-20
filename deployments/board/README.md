# Biplane board adapter

`biplane_board.py` is the policy-free client for the BIP-37 board service. It
reads `BIPLANE_API`, `BIPLANE_TOKEN` and the immutable
`BIPLANE_EXPECTED_USER_ID`; credentials are never written to its journal. The
server compares the expected id with the token-bound principal before claiming
an operation. The expectation is a mismatch guard, never asserted attribution.

For a transition it mints an operation key, persists the exact request under
`~/.local/state/biplane-board/operations/` before the first network call, and
prints the key before sending. If the result is unknown it queries the durable
server outcome before any retry. Resume an interrupted operation with:

```bash
python3 biplane_board.py resume <operation-key>
```

List reads page explicitly and exhaust the cursor before printing a result:

```bash
python3 biplane_board.py list netverse BIP
```

Read one stored work item after a write with:

```bash
python3 biplane_board.py get netverse BIP 37
```

The adapter contains no transition policy. Authentication, membership, scope,
locking, mutation, outcome and audit enforcement remain server-side.
