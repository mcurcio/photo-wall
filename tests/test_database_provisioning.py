"""The disposable test databases (support/database.py) and the server they run on."""

import re
from pathlib import Path

import psycopg
import pytest
from support import database as provisioning

from scripts.test_local import TEST_DATABASE_URL

ROOT = Path(__file__).parents[1]
TEST_COMPOSE = ROOT / "tests/integration/compose.test-database.yml"


def test_the_test_server_runs_the_deployment_postgres_image():
    def image(path):
        return re.search(r"image: (postgres:\S+@sha256:[0-9a-f]{64})", path.read_text())[1]

    assert image(TEST_COMPOSE) == image(ROOT / "compose.yaml")


def test_the_local_wrapper_names_the_test_server():
    text = TEST_COMPOSE.read_text()
    user = re.search(r"POSTGRES_USER: (\S+)", text)[1]
    password = re.search(r"POSTGRES_PASSWORD: (\S+)", text)[1]
    database = re.search(r"POSTGRES_DB: (\S+)", text)[1]
    port = re.search(r"\$\{PHOTO_WALL_TEST_DB_PORT:-(\d+)\}:5432", text)[1]
    assert TEST_DATABASE_URL.format(port=port) == (
        f"postgresql://{user}:{password}@127.0.0.1:{port}/{database}")


def test_a_database_test_skips_without_a_server_unless_one_is_required(monkeypatch):
    monkeypatch.delenv(provisioning.DSN_VARIABLE, raising=False)
    monkeypatch.delenv(provisioning.REQUIRE_VARIABLE, raising=False)
    with pytest.raises(pytest.skip.Exception):
        provisioning.server_dsn()
    monkeypatch.setenv(provisioning.REQUIRE_VARIABLE, "1")
    with pytest.raises(pytest.fail.Exception):
        provisioning.server_dsn()


def test_the_template_name_follows_every_migration_and_the_runner(tmp_path, monkeypatch):
    (tmp_path / "migrations").mkdir()
    runner = tmp_path / "db.py"
    runner.write_text("runner")
    migration = tmp_path / "migrations" / "001_a.sql"
    migration.write_text("CREATE TABLE a ();")
    monkeypatch.setattr(provisioning, "_RUNNER", runner)
    names = [provisioning.template_name()]
    migration.write_text("CREATE TABLE a (b int);")
    names.append(provisioning.template_name())
    (tmp_path / "migrations" / "002_b.sql").write_text("")
    names.append(provisioning.template_name())
    runner.write_text("runner, changed")
    names.append(provisioning.template_name())
    assert len(set(names)) == 4
    assert all(re.fullmatch(r"pw_tmpl_[0-9a-f]{16}", name) for name in names)


def test_each_test_owns_a_clone_of_a_published_template(database_provisioner, registry):
    template = provisioning.template_name()
    with psycopg.connect(database_provisioner.dsn, autocommit=True) as admin:
        assert admin.execute("SELECT datistemplate, datallowconn FROM pg_database "
                             "WHERE datname=%s", (template,)).fetchone() == (True, False)
    with registry.db.transaction() as conn:
        own = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        assert conn.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()["n"] == len(
            list((ROOT / "central/migrations").glob("*.sql")))
    assert re.fullmatch(r"pw_t_[0-9a-f]{12}_[0-9a-f]{8}_[0-9a-f]{12}", own)
    with database_provisioner.migrated() as other:
        with psycopg.connect(other) as conn:
            assert conn.execute("SELECT current_database()").fetchone()[0] != own
