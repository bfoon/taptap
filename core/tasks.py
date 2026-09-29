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
    from .notify import deliver
    deliver()


@shared_task(ignore_result=True, queue="live")
def process_inventory_piece(cmd_id):
    from .agent_inventory import process_piece
    process_piece(cmd_id)


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
