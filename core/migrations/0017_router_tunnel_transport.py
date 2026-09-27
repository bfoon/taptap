from django.db import migrations


POSTGRES_SQL = """
CREATE TABLE IF NOT EXISTS core_router_tunnel (
    router_id BIGINT PRIMARY KEY,
    tunnel_ip VARCHAR(64) NOT NULL UNIQUE,
    router_public_key VARCHAR(128) NOT NULL DEFAULT '',
    api_username VARCHAR(64) NOT NULL DEFAULT 'taptap-tunnel',
    api_password VARCHAR(160) NOT NULL DEFAULT '',
    status VARCHAR(32) NOT NULL DEFAULT 'waiting',
    last_handshake_at TIMESTAMPTZ NULL,
    last_api_ok_at TIMESTAMPTZ NULL,
    last_error TEXT NOT NULL DEFAULT '',
    last_repair_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS core_router_tunnel_status_idx
ON core_router_tunnel (status);
"""

SQLITE_SQL = """
CREATE TABLE IF NOT EXISTS core_router_tunnel (
    router_id INTEGER PRIMARY KEY,
    tunnel_ip VARCHAR(64) NOT NULL UNIQUE,
    router_public_key VARCHAR(128) NOT NULL DEFAULT '',
    api_username VARCHAR(64) NOT NULL DEFAULT 'taptap-tunnel',
    api_password VARCHAR(160) NOT NULL DEFAULT '',
    status VARCHAR(32) NOT NULL DEFAULT 'waiting',
    last_handshake_at DATETIME NULL,
    last_api_ok_at DATETIME NULL,
    last_error TEXT NOT NULL DEFAULT '',
    last_repair_at DATETIME NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS core_router_tunnel_status_idx
ON core_router_tunnel (status);
"""


def create_table(apps, schema_editor):
    sql = POSTGRES_SQL if schema_editor.connection.vendor == "postgresql" else SQLITE_SQL
    for statement in [part.strip() for part in sql.split(";") if part.strip()]:
        schema_editor.execute(statement)


def drop_table(apps, schema_editor):
    schema_editor.execute("DROP TABLE IF EXISTS core_router_tunnel")


class Migration(migrations.Migration):
    dependencies = [("core", "0016_link_script_version")]
    operations = [migrations.RunPython(create_table, drop_table)]
