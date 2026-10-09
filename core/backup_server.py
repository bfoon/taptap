"""Durable, private RouterOS backup storage for TapTap.

The MikroTik is only the source of the backup.  A short-lived one-time upload
token lets the router POST base64 file chunks back to TapTap over HTTPS.  The
server writes them to a private persistent Docker volume, verifies length and
SHA-256, and serves downloads only to authenticated business users.

Nothing in this module changes TapTap Link, routes, firewall, DNS or tunnel
configuration.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets
import shutil
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import RouterBackup
from .models_backup_storage import RouterBackupStorage

logger = logging.getLogger("taptap.backups")

TOKEN_MINUTES = 60
CHUNK_SIZE = 16384      # 16 KB per request: a 10 MB backup is ~650 requests, not ~3,400 (base64 stays far below Fetch's 64 KB)
MAX_BYTES = int(os.getenv("ROUTER_BACKUP_MAX_BYTES", str(32 * 1024 * 1024)))
PRIVATE_ROOT = Path(
    os.getenv(
        "ROUTER_BACKUP_ROOT",
        str(getattr(settings, "BASE_DIR", Path("/app")) / "router_backups"),
    )
)
SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def _root() -> Path:
    PRIVATE_ROOT.mkdir(parents=True, exist_ok=True)
    return PRIVATE_ROOT.resolve()


def _safe(value: str) -> str:
    value = SAFE_NAME.sub("-", str(value or "").strip()).strip(".-")
    return value[:100] or "router"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _relative_dir(backup: RouterBackup) -> Path:
    return Path(
        f"business-{backup.router.business_id}",
        f"router-{backup.router_id}",
        f"backup-{backup.pk}",
    )


def _full_path(relative: str | Path) -> Path:
    root = _root()
    full = (root / Path(relative)).resolve()
    if full != root and root not in full.parents:
        raise ValueError("Unsafe backup path.")
    return full


def _kind_info(backup: RouterBackup, kind: str):
    if kind == "backup":
        return {
            "extension": ".backup",
            "path_field": "backup_path",
            "size_field": "backup_size",
            "expected_field": "expected_backup_size",
            "sha_field": "backup_sha256",
            "filename": backup.backup_file or f"{backup.name}.backup",
        }
    if kind == "export":
        return {
            "extension": ".rsc",
            "path_field": "export_path",
            "size_field": "server_export_size",
            "expected_field": "expected_export_size",
            "sha_field": "export_sha256",
            "filename": backup.export_file or f"{backup.name}.rsc",
        }
    raise ValueError("Unknown backup file kind.")


def _server_relpath(backup: RouterBackup, kind: str) -> str:
    info = _kind_info(backup, kind)
    filename = _safe(Path(info["filename"]).name)
    if not filename.endswith(info["extension"]):
        filename += info["extension"]
    return str(_relative_dir(backup) / filename)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _base_url(request=None) -> str:
    url = str(getattr(settings, "SITE_URL", "") or "").rstrip("/")
    if not url and request is not None:
        url = request.build_absolute_uri("/").rstrip("/")
    if not url:
        raise ValueError(
            "SITE_URL is required for server backups so the MikroTik knows where to upload them."
        )
    return url


def _upload_url(backup: RouterBackup, request=None) -> str:
    return f"{_base_url(request)}/api/router-backups/{backup.pk}/upload/"


def _make_record(router, user=None, automatic=False):
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    base = _safe(f"taptap-{router.name}-{stamp}")
    record = RouterBackup.objects.create(
        router=router,
        name=base,
        backup_file=f"{base}.backup",
        export_file=f"{base}.rsc",
        automatic=automatic,
        created_by=user,
    )
    token = "ttb_" + secrets.token_urlsafe(32)
    storage = RouterBackupStorage.objects.create(
        backup=record,
        status="queued",
        upload_token_hash=_hash_token(token),
        upload_expires_at=timezone.now() + timedelta(minutes=TOKEN_MINUTES),
    )
    return record, storage, token


def _fail(record, storage, message):
    message = str(message or "Backup failed.")[:1000]
    RouterBackupStorage.objects.filter(pk=storage.pk).update(
        status="failed", error=message
    )
    RouterBackup.objects.filter(pk=record.pk).update(error=message[:255])


SERVER_BACKUP_MIN_ROS = (7, 13)      # /file read and :convert / :onerror, which the upload script needs


def ros_tuple(version):
    """'7.14.3 (stable)' -> (7, 14); None when unknown."""
    m = re.match(r"\s*(\d+)\.(\d+)", str(version or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def can_upload_to_server(version):
    """Unknown versions are tried (the router reports back if it cannot); known older ones use the router-side backup."""
    t = ros_tuple(version)
    return t is None or t >= SERVER_BACKUP_MIN_ROS


def _router_script(base: str, upload_url: str, token: str) -> str:
    """Fixed RouterOS script (RouterOS 7.13+): create both files, wait until they are written, upload them in safe
    chunks, and if anything fails tell TapTap why (the backup page shows the reason). It must fit one TapTap Link
    command; HTTPS trust for direct-API routers is prepared over the API first (_ensure_trust_api)."""
    from .agent import rs, tls_flag

    check = tls_flag(upload_url)
    b, url, tok = rs(base), rs(upload_url), rs(token)
    return f':local base {b}; :local url {url}; :local tok {tok}; ' + _script_body(check)


SAVE_STEP = ':do { /system backup save name=$base dont-encrypt=yes } on-error={ /system backup save name=$base }; /export file=$base; '
# TapTap Link cannot save a backup itself (needs "policy" and "password"): it runs the small local helper the owner
# pasted once, which only saves the two files. Everything else — waiting, uploading, reporting — runs in Link's own
# command, so fixes to the upload reach every router without pasting anything again.
# it reads :global ttBkBase (set by the command); "needs-helper" makes the Backups page show the one-time command
LINK_SAVE_STEP = ':if ([:len [/system script find name=taptap-backup-local]] = 0) do={ :error "needs-helper" }; /system script run taptap-backup-local; '


def _script_body(check, save=SAVE_STEP):
    """Everything after "who/where": works with $base, $url and $tok already set (inline, or in the helper)."""
    # 3 KB of file -> about 4 KB of base64: conservative for RouterOS variables and far below Fetch's 64 KB limit.
    main = (
        ':local h (\"X-TapTap-Backup: \" . $tok); ' f':local c \"{check}\"; '
        # tell TapTap why it failed (no-op if even that cannot reach TapTap)
        ':local rp do={ :do { /tool fetch url=$url http-method=post '
        'http-data=("kind=error&message=" . [:convert [:pick $msg 0 240] to=url]) '
        'http-header-field=$h output=none '
        'check-certificate=$c idle-timeout=15s } on-error={} }; '
        ':local sf do={ '
        ':local fid [/file find name=$f]; '
        ':if ([:len $fid] = 0) do={ :error ("missing: " . $f) }; '
        ':local z [/file get $fid size]; '
        ':if ($z < 1) do={ :error ("empty: " . $f) }; '
        # Read piece by piece. A refused read ("chunk out of file bounds": the size RouterOS reports can be a little
        # off) is retried with half the size; the position moves by the bytes really read, and an empty read after
        # the first piece is the real end of the file — TapTap is told the true length when finishing.
        ':local o 0; '
        ':while ($o < $z) do={ '
        f':local n {CHUNK_SIZE}; :if (($o + $n) > $z) do={{ :set n ($z - $o) }}; '
        ':local r ""; :local g 0; '
        ':while (($g = 0) and ($n > 0)) do={ '
        ':onerror e in={ :local rr [/file read file=$f offset=$o chunk-size=$n as-value]; '
        ':set r ($rr->"data"); :if ([:typeof $r] = "nil") do={ :set r (($rr->0)->"data") }; '
        ':set g [:len $r] } do={ :set g 0 }; '
        ':if ($g = 0) do={ :set n ($n / 2) } }; '
        ':if ($g = 0) do={ :if ($o = 0) do={ :error ("cannot read " . $f) }; :set z $o } else={ '
        ':local q ("kind=" . $kind . "&offset=" . $o . "&total=" . $z . '
        '"&data=" . [:convert [:convert $r to=base64] to=url]); '
        '/tool fetch url=$url http-method=post http-data=$q '
        'http-header-field=$h '
        'output=none check-certificate=$c idle-timeout=20s; '
        ':set o ($o + $g) } }; :return $o }; '
        ':onerror err in={ ' + save +
        ':local bf ($base . ".backup"); :local ef ($base . ".rsc"); '
        # wait until both files exist and stopped growing (slow flash can take several seconds), at most 45 s
        ':local rd false; :local i 0; :local lb -1; :local le -1; '
        ':while ((!$rd) and ($i < 45)) do={ :delay 1s; :set i ($i + 1); '
        ':local fb [/file find name=$bf]; :local fe [/file find name=$ef]; '
        ':if (([:len $fb] > 0) and ([:len $fe] > 0)) do={ '
        ':local sb [/file get $fb size]; :local se [/file get $fe size]; '
        ':if (($sb > 0) and ($se > 0) and ($sb = $lb) and ($se = $le)) do={ :set rd true }; '
        ':set lb $sb; :set le $se } }; '
        ':if (!$rd) do={ :error "files not written in 45 s" }; '
        ':local bs [$sf f=$bf kind="backup" url=$url h=$h c=$c]; '
        ':local es [$sf f=$ef kind="export" url=$url h=$h c=$c]; '
        ':local ver [/system resource get version]; '
        ':local done ("kind=finish&backup_total=" . $bs . "&export_total=" . $es . '
        '"&version=" . [:convert $ver to=url]); '
        ':local fin [/tool fetch url=$url http-method=post http-data=$done '
        'http-header-field=$h '
        'output=user as-value check-certificate=$c idle-timeout=20s]; '
        ':if (($fin->"status") != "finished") do={ :error "TapTap did not confirm" }; '
        '/file remove $bf; /file remove $ef; '
        ':log info "TapTap backup saved on server" '
        '} do={ :log warning ("TapTap backup: " . $err); [$rp url=$url h=$h c=$c msg=$err] }'
    )
    return main


# ─────────── the backup helper (TapTap Link) ───────────
# TapTap Link's own script runs with "ftp,read,write,test,reboot,sensitive". Saving a backup also needs "policy" and
# "password" (a backup holds the users and their passwords): RouterOS answers "not enough permissions (9)". Rather
# than giving Link those rights for everything, one small fixed script on the router does only the backup, with the
# rights it needs (dont-require-permissions=yes): Link just hands it the file name, upload address and one-time code.
HELPER_NAME = "taptap-backup"
HELPER_POLICY = "ftp,read,write,policy,test,password,sensitive"
HELPER_COMMENT = "TapTap backup helper v1 - lets TapTap Link make backups"
LOCAL_HELPER = "taptap-backup-local"            # RouterOS older than 7.13: save both files on the router only
LOCAL_SOURCE = (':global ttBkBase; :do { /system backup save name=$ttBkBase dont-encrypt=yes } '
                'on-error={ /system backup save name=$ttBkBase }; /export file=$ttBkBase')
STORAGE_FULL = ("The router could not write the backup file — its storage is almost certainly full (a backup needs "
                "room for the .backup and the .rsc). Open Clean router memory, save the old backups to your computer and remove "
                "them, then press Back up now again.")
NEEDS_HELPER = ("This router needs a one-time permission before TapTap Link can make backups: "
                "open Backups and paste the “Allow backups on this router” command into WinBox once.")


def helper_source(check):
    head = (':global ttBkBase; :global ttBkUrl; :global ttBkTok; '
            ':local base $ttBkBase; :local url $ttBkUrl; :local tok $ttBkTok; :set ttBkTok ""; ')
    return head + _script_body(check)


def helper_install_command(base_url=None):
    """RouterOS commands that put the helper on a router (WinBox → New Terminal, once; also part of quick install)."""
    from .agent import base_url as agent_base, rs, tls_flag
    url = (base_url or agent_base(None)).rstrip("/")
    check = tls_flag(url)
    return ("# TapTap: allow backups through TapTap Link (one small script that only makes backups)\n"
            f'/system script remove [find where name="{HELPER_NAME}"]\n'
            f'/system script add name="{HELPER_NAME}" policy={HELPER_POLICY} dont-require-permissions=yes '
            f'comment="{HELPER_COMMENT}" source={rs(helper_source(check))}\n'
            f'/system script remove [find where name="{LOCAL_HELPER}"]\n'
            f'/system script add name="{LOCAL_HELPER}" policy={HELPER_POLICY} dont-require-permissions=yes '
            f'comment="TapTap backup helper (on-router copy)" source={rs(LOCAL_SOURCE)}\n'
            ':put "TapTap backups allowed on this router"')


def link_backup_body(params):
    base = _safe(params.get("file"))
    url = str(params.get("upload_url") or "")
    token = str(params.get("upload_token") or "")
    if not base or not url.startswith(("https://", "http://")) or not token.startswith("ttb_"):
        raise ValueError("Invalid server-backup parameters.")
    from .agent import rs, tls_flag
    check, b, u, t = tls_flag(url), rs(base), rs(url), rs(token)
    # The save-only helper (pasted once with "Allow backups") saves the files; this command uploads them.
    # Without it, TapTap is told so and the Backups page shows the one-time command.
    # ttBkR: a router never runs the same backup twice at once (a late resend would re-save the file and clash
    # with the upload already under way — "409 Conflict").
    return (f':global ttBkBase {b}; :global ttBkR; :if ($ttBkR=$ttBkBase) do={{:error "busy"}}; :set ttBkR $ttBkBase; '
            f':local base $ttBkBase; :local url {u}; :local tok {t}; '
            + _script_body(check, LINK_SAVE_STEP))


def _queue_link(record, storage, token, user=None, request=None):
    from . import agent

    command = agent.queue(
        record.router,
        "server_backup",
        {
            "file": record.name,
            "backup_id": record.pk,
            "upload_url": _upload_url(record, request),
            "upload_token": token,
        },
        label="Back up configuration to TapTap server",
        user=user,
        minutes=TOKEN_MINUTES,
    )
    return command


def _ensure_trust_api(svc):
    """Direct-API routers often trust no root certificate, so their HTTPS upload to TapTap fails at once.
    Over the API (no size limit): switch on RouterOS's built-in trust store and import any of TapTap's carried roots
    the router does not have yet (core/link_trust.py). Every step is optional — a failure here never stops the backup."""
    try:
        svc.resource("/certificate/settings").call("set", {"builtin-trust-anchors": "trusted"})   # RouterOS 7.19+
    except Exception:
        pass
    try:
        from cryptography import x509
        from .link_trust import root_pems
        certs = svc.resource("/certificate")
        have = {str(r.get("common-name", "")) for r in certs.get()}
        files = svc.resource("/file")
    except Exception:
        return
    for i, pem in enumerate(root_pems()):
        try:
            cn = x509.load_pem_x509_certificate(pem.encode()).subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
        except Exception:
            continue
        if cn in have:
            continue
        name = f"taptap-ca-{i}.pem"
        try:
            files.add(name=name, contents=pem)
            try:
                certs.call("import", {"file-name": name, "passphrase": "", "trusted": "yes"})
            except Exception:
                certs.call("import", {"file-name": name, "passphrase": ""})
        except Exception as exc:
            logger.info("backup trust: could not import %s: %s", cn, exc)
        finally:
            try:
                for row in files.get(name=name):
                    files.remove(id=row.get("id") or row.get(".id"))
            except Exception:
                pass


def _schedule_direct(record, storage, token, svc, request=None):
    from .agent import tls_flag
    from .portctl import schedule_on_router

    if tls_flag(_upload_url(record, request)) != "no":
        _ensure_trust_api(svc)

    script = _router_script(record.name, _upload_url(record, request), token)
    schedule_on_router(
        svc,
        f"taptap-server-backup-{record.pk}",
        3,
        script,
    )


_LEGACY_BACKUP = None            # portctl.backup as it was before this module replaced it (works on RouterOS 6 and 7)


def _ros_of(router, svc=None):
    """RouterOS version: what TapTap Link last reported, or ask the router over the API."""
    agent = getattr(router, "agent", None) if getattr(router, "connection_mode", "api") == "agent" else None
    if agent is not None and agent.ros_version:
        return agent.ros_version
    if svc is not None:
        try:
            return str((svc.safe_get("/system/resource") or [{}])[0].get("version", ""))
        except Exception:
            return ""
    return ""


def _router_side_backup(router, user, automatic, version, svc=None):
    """RouterOS older than 7.13 cannot read its own files from a script, so it cannot upload them to TapTap.
    Direct API: the proven method (full backup kept on the router, text export copied to the TapTap server).
    TapTap Link: the router saves both files on itself."""
    note = (f"RouterOS {version.split()[0]} keeps the full .backup on the router (copying it to the TapTap server needs "
            f"RouterOS 7.13 or newer). ")
    if getattr(router, "connection_mode", "api") == "agent":
        from . import agent
        record = RouterBackup.objects.create(router=router, name=_safe(f"taptap-{router.name}-{timezone.localtime():%Y%m%d-%H%M%S}"),
                                             automatic=automatic, created_by=user, ros_version=version[:60])
        record.backup_file, record.export_file = f"{record.name}.backup", f"{record.name}.rsc"
        record.error = (note + "Both files are saved on the router (Files).")[:255]
        record.save(update_fields=["backup_file", "export_file", "error"])
        agent.queue(router, "backup", {"file": record.name}, label="Back up configuration on the router", user=user)
        type(router).objects.filter(pk=router.pk).update(last_backup_at=timezone.now())
        return record, None
    legacy = _LEGACY_BACKUP
    if legacy is None:
        raise ValueError("No router-side backup method available.")
    own = svc is None
    if own:
        from .mikrotik import MikroTikService
        svc = MikroTikService(router).connect()
    try:
        record = legacy(svc, router, user=user, automatic=automatic)
    finally:
        if own:
            svc.close()
    storage = _legacy_export_to_server(record) if getattr(record, "content", "") else None
    if storage is not None:
        RouterBackupStorage.objects.filter(pk=storage.pk).update(error=(note + "The text export is saved on the TapTap server.")[:1000])
    RouterBackup.objects.filter(pk=record.pk).update(error=(note + ("The text export is saved on the TapTap server." if storage else ""))[:255])
    return record, storage


def start_backup(router, user=None, automatic=False, request=None, svc=None):
    """Create a server-backed backup job. Returns (RouterBackup, storage).
    Routers on RouterOS older than 7.13 get the router-side backup instead (see _router_side_backup)."""
    # Resolve the public URL before making a DB record.
    _base_url(request)
    version = _ros_of(router, svc)
    if not can_upload_to_server(version):
        return _router_side_backup(router, user, automatic, version, svc)
    record, storage, token = _make_record(router, user, automatic)

    try:
        from .voucher_history import channel
        if getattr(router, "connection_mode", "api") == "agent" and channel(router) == "TapTap Link":
            _queue_link(record, storage, token, user=user, request=request)
        else:
            own = svc is None
            if own:
                from .mikrotik import MikroTikService

                svc = MikroTikService(router).connect()
            try:
                _schedule_direct(record, storage, token, svc, request=request)
            finally:
                if own:
                    svc.close()

        type(router).objects.filter(pk=router.pk).update(last_backup_at=timezone.now())
        return record, storage
    except Exception as exc:
        _fail(record, storage, exc)
        raise


def backup_with_service(svc, router, user=None, automatic=False):
    """Drop-in replacement for portctl.backup used by nightly/live backup."""
    record, _storage = start_backup(
        router,
        user=user,
        automatic=automatic,
        svc=svc,
    )
    return record


def _token_ok(storage: RouterBackupStorage, token: str) -> bool:
    if not token or not secrets.compare_digest(
        storage.upload_token_hash or "", _hash_token(token)
    ):
        return False
    return bool(
        storage.upload_expires_at
        and storage.upload_expires_at >= timezone.now()
    )


def _param(data, name, default=""):
    values = data.get(name)
    return values[0] if values else default


@csrf_exempt
@require_POST
def router_backup_upload(request, bid):
    """Unauthenticated router endpoint secured by a one-time per-backup token."""
    if len(request.body) > 128 * 1024:
        return JsonResponse({"ok": False, "message": "Chunk too large."}, status=413)

    try:
        body = request.body.decode("ascii")
        data = parse_qs(body, keep_blank_values=True)
    except Exception:
        return JsonResponse({"ok": False, "message": "Invalid upload body."}, status=400)

    token = request.headers.get("X-TapTap-Backup", "")
    try:
        with transaction.atomic():
            storage = (
                RouterBackupStorage.objects
                .select_for_update()
                .select_related("backup__router__business")
                .get(backup_id=bid)
            )
            if not _token_ok(storage, token):
                return JsonResponse({"ok": False, "message": "Backup token rejected."}, status=403)

            record = storage.backup
            kind = _param(data, "kind")

            if kind == "error":
                # The router could not finish: keep its reason for the backup page instead of waiting for the expiry.
                said = _param(data, "message")
                low = said.lower()
                if "needs-helper" in said or "not enough permissions" in low:
                    message = NEEDS_HELPER
                elif "action failed" in low and ("backup/save" in low or "export" in low):
                    message = STORAGE_FULL + f" (Router said: {said})"[:300]
                elif "409" in said and "conflict" in low:
                    message = ("The copy clashed with another copy of the same backup still uploading (an older TapTap sent "
                               "the backup twice). Run Back up now again — this version waits for the upload to finish.")
                else:
                    message = ("Router: " + said)[:1000]
                storage.status, storage.error = "failed", message
                storage.save(update_fields=["status", "error", "updated_at"])
                RouterBackup.objects.filter(pk=record.pk).update(error=message[:255])
                return JsonResponse({"ok": True, "status": "failed"})

            if kind == "finish":
                version = _param(data, "version")[:60]
                if version:
                    RouterBackup.objects.filter(pk=record.pk).update(ros_version=version)
                # The router reports how many bytes it really read from each file (RouterOS can report a file
                # size a little larger than it lets a script read). If that is exactly what TapTap holds, that is
                # the complete file.
                for kind_, param in (("backup", "backup_total"), ("export", "export_total")):
                    try:
                        real = int(_param(data, param) or 0)
                    except ValueError:
                        real = 0
                    info = _kind_info(record, kind_)
                    rel = getattr(storage, info["path_field"])
                    if real > 0 and rel:
                        full = _full_path(rel)
                        if full.exists() and full.stat().st_size == real and getattr(storage, info["size_field"]) != real:
                            setattr(storage, info["expected_field"], real)
                            setattr(storage, info["size_field"], real)
                            setattr(storage, info["sha_field"], _sha256_file(full))
                            storage.save(update_fields=[info["expected_field"], info["size_field"], info["sha_field"], "updated_at"])

                backup_ok = bool(
                    storage.backup_path
                    and storage.backup_size
                    and storage.backup_size == storage.expected_backup_size
                )
                export_ok = bool(
                    storage.export_path
                    and storage.server_export_size
                    and storage.server_export_size == storage.expected_export_size
                )
                if not (backup_ok and export_ok):
                    storage.status = "partial"
                    storage.error = "Router finished, but TapTap did not receive both complete files."
                    storage.save(update_fields=["status", "error", "updated_at"])
                    return JsonResponse({"ok": False, "message": storage.error}, status=409)

                was_ready = storage.status == "ready"
                storage.status = "ready"
                storage.error = ""
                storage.completed_at = storage.completed_at or timezone.now()
                storage.save(update_fields=["status", "error", "completed_at", "updated_at"])
                RouterBackup.objects.filter(pk=record.pk).update(error="")
                notify_now = not was_ready
                response_status = "ready"
            else:
                if kind not in {"backup", "export"}:
                    return JsonResponse({"ok": False, "message": "Unknown file kind."}, status=400)

                try:
                    offset = int(_param(data, "offset"))
                    total = int(_param(data, "total"))
                    raw = _param(data, "data").replace(" ", "+").replace("\n", "").replace("\r", "")
                    raw = raw.replace("-", "+").replace("_", "/")          # also accept URL-safe base64
                    chunk = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)
                except Exception:
                    return JsonResponse({"ok": False, "message": "Invalid chunk."}, status=400)

                if offset < 0 or total < 1 or total > MAX_BYTES or len(chunk) > CHUNK_SIZE + 1024:
                    return JsonResponse({"ok": False, "message": "Invalid backup size."}, status=400)
                if offset + len(chunk) > total:
                    return JsonResponse({"ok": False, "message": "Chunk exceeds declared file size."}, status=400)

                info = _kind_info(record, kind)
                rel = getattr(storage, info["path_field"]) or _server_relpath(record, kind)
                full = _full_path(rel)
                full.parent.mkdir(parents=True, exist_ok=True)
                current = full.stat().st_size if full.exists() else 0

                if offset > current:
                    return JsonResponse(
                        {"ok": False, "message": f"Expected offset {current}, got {offset}."},
                        status=409,
                    )

                if offset < current:
                    # Safe retry: accept only if the bytes already on disk are identical.
                    with full.open("rb") as handle:
                        handle.seek(offset)
                        existing = handle.read(len(chunk))
                    if existing != chunk:
                        return JsonResponse(
                            {"ok": False, "message": "Retry data does not match server copy."},
                            status=409,
                        )
                else:
                    with full.open("ab") as handle:
                        handle.write(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())

                current = full.stat().st_size
                setattr(storage, info["path_field"], rel)
                setattr(storage, info["expected_field"], total)
                storage.status = "uploading"
                storage.error = ""

                fields = [
                    info["path_field"],
                    info["expected_field"],
                    "status",
                    "error",
                    "updated_at",
                ]
                if current == total:
                    setattr(storage, info["size_field"], current)
                    setattr(storage, info["sha_field"], _sha256_file(full))
                    fields.extend([info["size_field"], info["sha_field"]])

                # If both files are already complete, the final call will simply confirm it.
                other_complete = (
                    storage.server_export_size == storage.expected_export_size
                    and storage.server_export_size > 0
                    if kind == "backup"
                    else storage.backup_size == storage.expected_backup_size
                    and storage.backup_size > 0
                )
                old_status = storage.status
                if current == total and other_complete:
                    storage.status = "ready"
                    storage.completed_at = storage.completed_at or timezone.now()
                    fields.extend(["completed_at"])
                storage.save(update_fields=list(dict.fromkeys(fields)))
                notify_now = storage.status == "ready" and old_status != "ready"
                response_status = storage.status

        if notify_now:
            _after_ready(record.pk)

        return JsonResponse(
            {
                "ok": True,
                "status": response_status,
            }
        )

    except RouterBackupStorage.DoesNotExist:
        return JsonResponse({"ok": False, "message": "Backup job not found."}, status=404)
    except Exception as exc:
        logger.exception("router backup upload %s failed", bid)
        return JsonResponse({"ok": False, "message": str(exc)[:300]}, status=500)


def _after_ready(backup_id):
    try:
        record = RouterBackup.objects.select_related("router__business").get(pk=backup_id)
        # Notify only after the durable server copy exists.
        from .notify import notify

        notify(
            record.router.business,
            "backup_done",
            f"Backup saved on server for {record.router.name}",
            f"{record.backup_file} and {record.export_file} are now stored privately on the TapTap server.",
            link="/topology/",
        )
        # TapTap historically keeps the newest 30 records.
        old = list(
            record.router.backups.order_by("-created_at")
            .values_list("pk", flat=True)[30:]
        )
        if old:
            RouterBackup.objects.filter(pk__in=old).delete()
    except Exception:
        logger.exception("backup completion housekeeping failed for %s", backup_id)


def _expire_stale(storage):
    if not storage or storage.status not in {"queued", "uploading"}:
        return storage

    try:
        from .models import AgentCommand
        cmd = (
            AgentCommand.objects
            .filter(
                router_id=storage.backup.router_id,
                kind="server_backup",
                params__backup_id=storage.backup_id,
            )
            .order_by("-created_at")
            .first()
        )
        if cmd and cmd.status in {"failed", "expired", "cancelled"}:
            storage.status = "failed"
            storage.error = cmd.result or "The router reported an error while creating or uploading the backup."
            storage.save(update_fields=["status", "error", "updated_at"])
            RouterBackup.objects.filter(pk=storage.backup_id).update(error=storage.error[:255])
            return storage
    except Exception:
        pass

    if storage.upload_expires_at and storage.upload_expires_at < timezone.now():
        storage.status = "failed"
        storage.error = "The router did not finish copying the backup to the TapTap server before the upload token expired."
        storage.save(update_fields=["status", "error", "updated_at"])
        RouterBackup.objects.filter(pk=storage.backup_id).update(error=storage.error[:255])
    return storage


def _legacy_export_to_server(record):
    """Keep an old DB-stored .rsc as a real private server file."""
    if not record.content:
        return None
    try:
        storage = record.server_storage
        return storage
    except RouterBackupStorage.DoesNotExist:
        pass

    rel = _server_relpath(record, "export")
    full = _full_path(rel)
    full.parent.mkdir(parents=True, exist_ok=True)
    payload = record.content.encode("utf-8")
    full.write_bytes(payload)
    return RouterBackupStorage.objects.create(
        backup=record,
        status="partial",
        export_path=rel,
        expected_export_size=len(payload),
        server_export_size=len(payload),
        export_sha256=hashlib.sha256(payload).hexdigest(),
        error="Legacy record: text export recovered to server; binary .backup was not previously copied from the router.",
    )


def _business_router(request, pk):
    business = getattr(request, "tt_business", None) or request.user.business
    return get_object_or_404(business.routers, pk=pk)


@login_required
def router_backups_server(request, pk):
    router = _business_router(request, pk)

    if request.method == "POST":
        action = request.POST.get("action", "backup")
        if action == "auto":
            router.auto_backup = request.POST.get("on") == "1"
            router.save(update_fields=["auto_backup"])
            return JsonResponse(
                {
                    "ok": True,
                    "message": "Nightly backups are "
                    + ("on (between 02:00 and 05:00)." if router.auto_backup else "off."),
                    "auto_backup": router.auto_backup,
                }
            )
        if action != "backup":
            return JsonResponse({"ok": False, "message": "Unknown backup action."}, status=400)

        try:
            record, storage = start_backup(
                router,
                user=request.user,
                request=request,
            )
            from .utils import log

            log(
                router.business,
                "Router Backup",
                f"{router.name}: server backup {record.name} started",
            )
            router_side = storage is None or storage.status == "partial"
            return JsonResponse(
                {
                    "ok": True,
                    "backup_id": record.pk,
                    "status": storage.status if storage is not None else "router",
                    "message": (
                        RouterBackup.objects.get(pk=record.pk).error or "Backup saved on the router."
                        if router_side else
                        "Backup started. TapTap is copying the binary .backup and text .rsc "
                        "into private persistent server storage."
                    ),
                }
            )
        except Exception as exc:
            return JsonResponse({"ok": False, "message": str(exc)[:300]}, status=502)

    items = []
    ready = 0
    for record in router.backups.all()[:30]:
        try:
            storage = _expire_stale(record.server_storage)
        except RouterBackupStorage.DoesNotExist:
            storage = _legacy_export_to_server(record)

        if storage:
            status = storage.status
            if status == "ready":
                ready += 1
            backup_url = (
                reverse("router_backup_server_download", args=[router.pk, record.pk, "backup"])
                if storage.has_backup
                else ""
            )
            export_url = (
                reverse("router_backup_server_download", args=[router.pk, record.pk, "export"])
                if storage.has_export
                else ""
            )
            storage_error = storage.error
            server_backup_size = storage.backup_size
            server_export_size = storage.server_export_size
            backup_sha = storage.backup_sha256
            export_sha = storage.export_sha256
        else:
            status = "legacy"
            backup_url = export_url = ""
            storage_error = (
                "Legacy backup record: no durable server copy exists. Create a new backup."
            )
            server_backup_size = server_export_size = 0
            backup_sha = export_sha = ""

        items.append(
            {
                "id": record.id,
                "name": record.name,
                "at": record.created_at.isoformat(),
                "backup_file": record.backup_file,
                "export_file": record.export_file,
                "automatic": record.automatic,
                "error": record.error,
                "version": record.ros_version,
                "server_status": status,
                "server_status_label": dict(RouterBackupStorage.STATUS).get(status, "Legacy"),
                "server_error": storage_error,
                "saved_on_server": status == "ready",
                "server_backup_size": server_backup_size,
                "server_export_size": server_export_size,
                "backup_sha256": backup_sha,
                "export_sha256": export_sha,
                "download_backup_url": backup_url,
                "download_export_url": export_url,
            }
        )

    # TapTap Link routers need the backup helper once: offer the command, and say so when a backup failed for it
    link = getattr(router, "connection_mode", "api") == "agent"
    helper_needed = link and any(NEEDS_HELPER[:40] in str(i.get("server_error") or i.get("error") or "")
                                 or "not enough permissions" in str(i.get("server_error") or i.get("error") or "").lower()
                                 for i in items[:3])
    return JsonResponse(
        {
            "ok": True,
            "items": items,
            "auto_backup": router.auto_backup,
            "server_ready": ready,
            "server_root": "private persistent TapTap storage",
            "helper_needed": helper_needed,
            "helper_command": helper_install_command(_base_url(request)) if link else "",
        }
    )


@login_required
def router_backup_server_download(request, pk, bid, kind):
    router = _business_router(request, pk)
    record = get_object_or_404(router.backups, pk=bid)
    try:
        storage = record.server_storage
    except RouterBackupStorage.DoesNotExist:
        raise Http404("This backup was not saved on the server.")

    if kind not in {"backup", "export"}:
        raise Http404("Unknown backup file.")
    info = _kind_info(record, kind)
    rel = getattr(storage, info["path_field"])
    if not rel:
        raise Http404("That file is not available on the server.")

    full = _full_path(rel)
    if not full.is_file():
        raise Http404("Server backup file is missing.")

    content_type = "application/octet-stream" if kind == "backup" else "text/plain; charset=utf-8"
    return FileResponse(
        full.open("rb"),
        as_attachment=True,
        filename=Path(rel).name,
        content_type=content_type,
    )


@receiver(post_delete, sender=RouterBackupStorage, dispatch_uid="taptap_backup_storage_cleanup")
def _delete_private_files(sender, instance, **kwargs):
    try:
        candidates = [x for x in (instance.backup_path, instance.export_path) if x]
        directories = {_full_path(Path(rel).parent) for rel in candidates}
        for directory in directories:
            if directory.exists():
                shutil.rmtree(directory)
    except Exception:
        logger.exception("Could not remove private backup files for %s", instance.pk)


def _install_agent_command():
    from . import agent
    from .models import AgentCommand

    agent.SAFE_KINDS.add("server_backup")
    agent.DEFAULT_EXPIRY["server_backup"] = TOKEN_MINUTES
    agent.ACK_WAIT["server_backup"] = 900

    original_body = agent.command_body
    if not getattr(original_body, "_taptap_server_backup", False):
        def command_body(cmd):
            if cmd.kind == "server_backup":
                return link_backup_body(cmd.params or {})
            return original_body(cmd)

        command_body._taptap_server_backup = True
        agent.command_body = command_body

    original_ack = agent.handle_ack
    if not getattr(original_ack, "_taptap_server_backup", False):
        def handle_ack(cmd_id, given_nonce, status, result=""):
            cmd = AgentCommand.objects.filter(pk=cmd_id, kind="server_backup").first()
            ok = original_ack(cmd_id, given_nonce, status, result)
            if not ok or not cmd:
                return ok

            cmd.refresh_from_db(fields=["status", "result", "params"])
            backup_id = (cmd.params or {}).get("backup_id")
            storage = RouterBackupStorage.objects.filter(backup_id=backup_id).select_related("backup").first() if backup_id else None
            if storage and cmd.status in {"failed", "expired", "cancelled"}:
                _fail(storage.backup, storage, cmd.result or "The router reported an error while creating or uploading the backup.")

            # Once the router has acknowledged the command, the one-time upload
            # token no longer needs to remain in the command log.
            params = dict(cmd.params or {})
            if "upload_token" in params:
                params["upload_token"] = "••redacted••"
                AgentCommand.objects.filter(pk=cmd.pk).update(params=params)
            return ok

        handle_ack._taptap_server_backup = True
        agent.handle_ack = handle_ack


def _install_backup_engine():
    # Nightly backup in portctl.tick() resolves this module global at runtime,
    # so replacing it here also makes automatic backups server-backed.
    from . import portctl, views_ports

    global _LEGACY_BACKUP
    if portctl.backup is not backup_with_service:          # keep the original for RouterOS older than 7.13
        _LEGACY_BACKUP = portctl.backup
    portctl.backup = backup_with_service
    views_ports.do_backup = backup_with_service

    # The visual designer imported backup logic dynamically. Point its helper at
    # this server-backed start function so pre-change backups are durable too.
    try:
        from . import control_designer

        def _backup_before_change(router, user):
            record, storage = start_backup(router, user=user)
            return {
                "transport": "TapTap Link" if router.connection_mode == "agent" else "RouterOS/API",
                "queued": True,
                "message": (
                    f"Full server backup {record.name} started before the configuration change."
                    if storage is not None and storage.status != "partial"
                    else f"Backup {record.name} saved on the router before the configuration change."
                ),
                "backup_id": record.pk,
                "server_status": storage.status if storage is not None else "router",
            }

        control_designer._backup_before_change = _backup_before_change
    except Exception:
        logger.exception("Could not attach server backup helper to visual designer")


def _install_urls():
    from . import urls

    # Replace the existing history/start endpoint while preserving its route/name,
    # so existing Control Center JavaScript keeps working.
    for index, pattern_obj in enumerate(list(urls.urlpatterns)):
        if getattr(pattern_obj, "name", None) == "router_backups":
            urls.urlpatterns[index] = path(
                "routers/<int:pk>/backups/",
                router_backups_server,
                name="router_backups",
            )
            break

    names = {getattr(p, "name", None) for p in urls.urlpatterns}
    if "router_backup_upload" not in names:
        urls.urlpatterns.append(
            path(
                "api/router-backups/<int:bid>/upload/",
                router_backup_upload,
                name="router_backup_upload",
            )
        )
    if "router_backup_server_download" not in names:
        urls.urlpatterns.append(
            path(
                "routers/<int:pk>/backups/<int:bid>/server/<str:kind>/download/",
                router_backup_server_download,
                name="router_backup_server_download",
            )
        )


def install():
    try:
        from . import permissions

        permissions.URL_PERMS["router_backup_server_download"] = "network.manage"
    except Exception:
        pass

    _install_agent_command()
    _install_backup_engine()
    _install_urls()
