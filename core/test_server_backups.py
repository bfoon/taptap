import base64
import hashlib
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from . import agent
from . import backup_server as bs


class ServerBackupScriptTests(SimpleTestCase):
    def test_server_backup_command_is_safe_catalogue_command(self):
        self.assertIn("server_backup", agent.SAFE_KINDS)

    def test_script_creates_and_uploads_both_routeros_files(self):
        script = bs._router_script(
            "taptap-Test-20261006-190000",
            "https://taptapnetwork.com/api/router-backups/42/upload/",
            "ttb_" + "x" * 43,
        )
        self.assertIn("/system backup save", script)
        self.assertIn("/export file=$base", script)
        self.assertIn("/file read", script)
        self.assertIn("to=base64", script)
        self.assertIn('kind="backup"', script)
        self.assertIn('kind="export"', script)
        self.assertIn("X-TapTap-Backup", script)
        self.assertIn("kind=finish", script)
        self.assertIn("/file remove", script)

    def test_script_fits_taptap_link_command_limit(self):
        script = bs._router_script(
            "taptap-Long-Router-Name-20261006-190000",
            "https://taptapnetwork.com/api/router-backups/999999/upload/",
            "ttb_" + "y" * 43,
        )
        # Leave headroom for TapTap Link's :do wrapper and ACK request.
        self.assertLess(len(script), agent.MAX_SCRIPT - 700)

    def test_upload_token_is_hashed(self):
        token = "ttb_secret-value"
        self.assertEqual(bs._hash_token(token), hashlib.sha256(token.encode()).hexdigest())
        self.assertNotEqual(bs._hash_token(token), token)

    def test_private_path_never_escapes_root(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(bs, "PRIVATE_ROOT", Path(td)):
                safe = bs._full_path("business-1/router-2/backup-3/a.backup")
                self.assertTrue(str(safe).startswith(str(Path(td).resolve())))
                with self.assertRaises(ValueError):
                    bs._full_path("../../etc/passwd")

    def test_router_files_are_removed_only_after_server_finish(self):
        script = bs._router_script(
            "taptap-Test",
            "https://example.test/api/router-backups/1/upload/",
            "ttb_" + "z" * 43,
        )
        finish = script.index("kind=finish")
        remove = script.index("/file remove")
        self.assertGreater(remove, finish)


class BackupChunkRulesTests(SimpleTestCase):
    def test_chunk_size_stays_below_routeros_file_read_limit(self):
        self.assertGreater(bs.CHUNK_SIZE, 0)
        self.assertLessEqual(bs.CHUNK_SIZE, 32768)

    def test_base64_chunk_stays_well_below_fetch_http_data_limit(self):
        encoded = base64.b64encode(b"x" * bs.CHUNK_SIZE)
        # RouterOS Fetch supports a 64 KB HTTP data variable; keep substantial headroom.
        self.assertLess(len(encoded) + 2048, 64 * 1024)
