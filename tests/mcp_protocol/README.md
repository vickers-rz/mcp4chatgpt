# mcpc protocol acceptance

Install the pinned client in this isolated directory with `npm ci`. Run
`MCP_MCPC_TESTS=1 PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_mcpc_acceptance.py`.
The test uses a temporary loopback HTTP service and `MCPC_HOME_DIR` under pytest's
temporary directory. It does not read or modify the user's mcpc sessions, profiles,
or keychain. It uses a test bearer token; OAuth browser login is not covered.
