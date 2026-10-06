from django.db import models


class RouterBackupStorage(models.Model):
    """Private server-side storage state for one RouterBackup.

    RouterBackup is the long-standing audit record.  This companion model records
    the durable copy held by TapTap's private backup volume.
    """

    STATUS = [
        ("queued", "Queued on router"),
        ("uploading", "Copying to server"),
        ("ready", "Saved on server"),
        ("partial", "Partly saved on server"),
        ("failed", "Server copy failed"),
        ("legacy", "Legacy router-only record"),
    ]

    backup = models.OneToOneField(
        "core.RouterBackup",
        on_delete=models.CASCADE,
        related_name="server_storage",
    )
    status = models.CharField(max_length=20, choices=STATUS, default="queued", db_index=True)

    # Relative paths beneath the private ROUTER_BACKUP_ROOT. Never expose these
    # directly through nginx; downloads go through an authenticated Django view.
    backup_path = models.CharField(max_length=500, blank=True)
    export_path = models.CharField(max_length=500, blank=True)

    expected_backup_size = models.BigIntegerField(default=0)
    expected_export_size = models.BigIntegerField(default=0)
    backup_size = models.BigIntegerField(default=0)
    server_export_size = models.BigIntegerField(default=0)
    backup_sha256 = models.CharField(max_length=64, blank=True)
    export_sha256 = models.CharField(max_length=64, blank=True)

    # Only a SHA-256 of the one-time router upload token is stored.
    upload_token_hash = models.CharField(max_length=64, blank=True)
    upload_expires_at = models.DateTimeField(null=True, blank=True)

    error = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.backup.name}: {self.get_status_display()}"

    @property
    def saved_on_server(self):
        return self.status == "ready" and bool(self.backup_path or self.export_path)

    @property
    def has_backup(self):
        return bool(self.backup_path and self.backup_size)

    @property
    def has_export(self):
        return bool(self.export_path and self.server_export_size)
