"""Operator access (decision 0035): who may call `/v1/operator/`, pure. The vocabulary is
`model.py`, every rule `policy.py`, the deployment's provider settings `settings.py`, the ports
`ports.py`. No HTTP client, database, FastAPI or JWT library is imported here (import-linter
contract "Operator access is pure")."""
