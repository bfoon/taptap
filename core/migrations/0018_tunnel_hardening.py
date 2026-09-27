"""TapTap Tunnel hardening.

- api_port: the router's real RouterOS API port (reported at registration).
- repair_attempts: drives exponential backoff for Link-driven repairs.
- api_password: widened and encrypted at rest (Fernet, key from
  TAPTAP_TUNNEL_SECRET or SECRET_KEY).
"""
import django.utils.timezone
from django.db import migrations, models


def _columns(schema_editor):
    with schema_editor.connection.cursor() as cur:
        return {
            c.name
            for c in schema_editor.connection.introspection.get_table_description(
                cur, "core_router_tunnel"
            )
        }


def forwards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    existing = _columns(schema_editor)
    if "api_port" not in existing:
        schema_editor.execute(
            "ALTER TABLE core_router_tunnel ADD COLUMN api_port INTEGER NOT NULL DEFAULT 8728"
        )
    if "repair_attempts" not in existing:
        schema_editor.execute(
            "ALTER TABLE core_router_tunnel ADD COLUMN repair_attempts INTEGER NOT NULL DEFAULT 0"
        )
    if vendor == "postgresql":
        schema_editor.execute(
            "ALTER TABLE core_router_tunnel ALTER COLUMN api_password TYPE VARCHAR(512)"
        )

    from core.tunnel import ENC_PREFIX, encrypt_secret

    with schema_editor.connection.cursor() as cur:
        cur.execute("SELECT router_id, api_password FROM core_router_tunnel")
        rows = cur.fetchall()
        for router_id, password in rows:
            if password and not str(password).startswith(ENC_PREFIX):
                cur.execute(
                    "UPDATE core_router_tunnel SET api_password = %s WHERE router_id = %s",
                    [encrypt_secret(password), router_id],
                )


def backwards(apps, schema_editor):
    from core.tunnel import ENC_PREFIX, decrypt_secret

    with schema_editor.connection.cursor() as cur:
        cur.execute("SELECT router_id, api_password FROM core_router_tunnel")
        for router_id, password in cur.fetchall():
            if password and str(password).startswith(ENC_PREFIX):
                cur.execute(
                    "UPDATE core_router_tunnel SET api_password = %s WHERE router_id = %s",
                    [decrypt_secret(password), router_id],
                )


class Migration(migrations.Migration):
    dependencies = [("core", "0017_router_tunnel_transport")]
    operations = [
        migrations.RunPython(forwards, backwards),
        # The table is created/altered by raw SQL above; this only registers the
        # unmanaged model in migration state so `makemigrations` stays clean.
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="RouterTunnel",
                    fields=[
                        ("router_id", models.BigIntegerField(primary_key=True, serialize=False)),
                        ("tunnel_ip", models.GenericIPAddressField(unique=True)),
                        ("router_public_key", models.CharField(blank=True, max_length=128)),
                        ("api_username", models.CharField(default="taptap-tunnel", max_length=64)),
                        ("api_password", models.CharField(max_length=512)),
                        ("api_port", models.IntegerField(default=8728)),
                        ("status", models.CharField(default="waiting", max_length=32)),
                        ("last_handshake_at", models.DateTimeField(blank=True, null=True)),
                        ("last_api_ok_at", models.DateTimeField(blank=True, null=True)),
                        ("last_error", models.TextField(blank=True)),
                        ("last_repair_at", models.DateTimeField(blank=True, null=True)),
                        ("repair_attempts", models.IntegerField(default=0)),
                        ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                        ("updated_at", models.DateTimeField(auto_now=True)),
                    ],
                    options={"db_table": "core_router_tunnel", "managed": False},
                ),
            ],
        ),
    ]
