from celery import shared_task
from django.utils import timezone

from .models import Router, RouterSyncJob
from .sync import sync_router


ACTIVE_SYNC_STATES = ("queued", "running")


def enqueue_router_sync(router, requested_by=None):
    """Queue one router if it does not already have a pending/running job."""
    existing = router.sync_jobs.filter(status__in=ACTIVE_SYNC_STATES).order_by("-created_at").first()
    if existing:
        return existing, False

    job = RouterSyncJob.objects.create(
        business=router.business,
        router=router,
        requested_by=requested_by,
        status="queued",
        progress=0,
        phase="Queued — waiting for background worker",
    )
    try:
        result = sync_router_task.delay(job.pk)
    except Exception as exc:
        job.status = "failed"
        job.phase = "Could not queue background job"
        job.error = str(exc)
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "phase", "error", "finished_at", "updated_at"])
        raise
    job.celery_task_id = result.id
    job.save(update_fields=["celery_task_id", "updated_at"])
    return job, True


def _complete_job(job, summary):
    warning_count = len(summary.get("errors") or [])
    job.status = "success"
    job.progress = 100
    job.phase = "Completed" + (f" with {warning_count} warning(s)" if warning_count else "")
    job.summary = summary
    job.error = ""
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "progress", "phase", "summary", "error", "finished_at", "updated_at"])
    try:    # member profiles that sign idle members out are corrected right after a sync
        from .member_keepalive import ensure_member_profiles
        ensure_member_profiles(job.router)
    except Exception:
        pass
    return summary


@shared_task(bind=True, acks_late=True)
def sync_router_task(self, job_id):
    """
    Agent routers use TapTap Tunnel first and automatically fall back to the
    existing TapTap Link inventory path if the tunnel is unhealthy.
    """
    job = RouterSyncJob.objects.select_related("router", "business").get(pk=job_id)
    router = job.router
    job.status = "running"
    job.progress = max(job.progress, 1)
    job.phase = "Background worker started"
    job.started_at = timezone.now()
    job.error = ""
    job.save(update_fields=["status", "progress", "phase", "started_at", "error", "updated_at"])

    def progress(percent, phase):
        RouterSyncJob.objects.filter(pk=job.pk).update(
            progress=percent,
            phase=phase,
            updated_at=timezone.now(),
        )

    try:
        if router.connection_mode == "agent":
            from .tunnel import mark_tunnel_error, tunnel_ready

            if tunnel_ready(router):
                progress(3, "TapTap Tunnel online — opening RouterOS API")
                try:
                    summary = sync_router(router, progress=progress)
                    return _complete_job(job, summary)
                except Exception as tunnel_exc:
                    mark_tunnel_error(router, tunnel_exc)
                    progress(5, "Tunnel unavailable — falling back to TapTap Link")

            from .agent_inventory import start_agent_inventory_sync
            return start_agent_inventory_sync(job)

        summary = sync_router(router, progress=progress)
        return _complete_job(job, summary)

    except Exception as exc:
        now = timezone.now()
        Router.objects.filter(pk=router.pk).exclude(connection_mode="agent").update(
            status="Offline", last_error=str(exc), last_tested_at=now
        )
        job.status = "failed"
        job.phase = "Synchronization failed"
        job.error = str(exc)
        job.finished_at = now
        job.save(update_fields=["status", "phase", "error", "finished_at", "updated_at"])
        try:
            from .notify import notify
            notify(
                router.business,
                "sync_failed",
                f"Sync failed for {router.name}",
                f"{router.name}: {str(exc)[:300]}",
                link="/routers/",
                key=f"sync:{router.pk}",
            )
        except Exception:
            pass
        raise


@shared_task(ignore_result=True)
def live_watch_all():
    from .live import watch_all
    watch_all()


@shared_task(ignore_result=True)
def deliver_notifications():
    """Deliver business notifications and opt-in member expiry reminders."""
    from .notify import deliver
    from .member_notifications import send_due_member_reminders

    deliver()
    send_due_member_reminders()
    try:                                   # scheduled member pause / unpause (core/member_support.py)
        from .member_support import run_due
        run_due()
    except Exception:
        import logging
        logging.getLogger('taptap.members').exception('member schedules failed')


@shared_task(ignore_result=True, queue="live")
def process_inventory_piece(cmd_id):
    from .agent_inventory import process_piece
    process_piece(cmd_id)


@shared_task(ignore_result=True)
def record_inventory_job(router_id, job_id, errors=None):
    """Note missing Link inventory tables on their sync job (kept out of the router's check-in)."""
    from .agent_inventory import record_job
    record_job(router_id, job_id, errors)


@shared_task(ignore_result=True)
def settle_link_router(router_id):
    """Finish stalled or overlong Link syncs for one router (kept out of the router's check-in)."""
    from .agent_inventory import settle_router
    router = Router.objects.select_related('business').filter(pk=router_id).first()
    if router:
        settle_router(router)


@shared_task(ignore_result=True)
def prune_agent_commands():
    """Hourly: delete old finished TapTap Link commands so Link bookkeeping stays fast."""
    from .agent_inventory import prune_commands
    prune_commands()


@shared_task(ignore_result=True)
def redeploy_portal(business_id):
    """Re-install the voucher portal on routers that have it (after page, plan or branding changes)."""
    from .models import Business
    from .portal_deploy import deploy
    business = Business.objects.filter(pk=business_id).first()
    if not business:
        return
    for dep in business.portal_deployments.select_related('router').filter(status__in=['installed', 'queued', 'failed']):
        deploy(dep.router)


@shared_task(ignore_result=True)
def chat_email_missed():
    """Email chat messages people have not read (Chat settings → email me unread messages)."""
    from .chat import email_missed
    return email_missed()


@shared_task(ignore_result=True)
def chat_purge_images():
    """Delete chat photos older than CHAT_IMAGE_DAYS (7) for good — hourly."""
    from .chat_media import purge_expired
    return purge_expired()


# Vouchers first: their own queue and worker ("vouchers" in docker-compose.yml), so a voucher change never
# waits behind a full sync (default worker) or the all-router live loop ("live" worker). The live worker also
# listens to "vouchers", so nothing is stranded where the dedicated worker is not deployed yet.
VOUCHER_QUEUE = "vouchers"


@shared_task(ignore_result=True, queue=VOUCHER_QUEUE)
def push_vouchers_task(router_id, voucher_ids):
    """Send only these vouchers to one router (no full sync)."""
    from .models import Router
    from .voucher_push import push_now
    router = Router.objects.filter(pk=router_id).first()
    if router:
        return push_now(router, voucher_ids)


@shared_task(ignore_result=True, queue=VOUCHER_QUEUE)
def forget_mac_task(voucher_id, mac):
    """Remove a locked phone's old MAC (session + cookie) from the router after it changed MAC."""
    from .device_lock import forget_mac_now
    from .models import Voucher
    v = Voucher.objects.filter(pk=voucher_id).select_related('router').first()
    if v and v.router_id:
        forget_mac_now(v, mac)


@shared_task(ignore_result=True)
def investor_tick():
    """Investors: book finished days of earnings, then send the daily summaries that are due (core/investors.py)."""
    from .investors import tick
    return tick()
