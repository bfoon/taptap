from celery import shared_task
from django.utils import timezone

from .models import Router, RouterSyncJob
from .sync import sync_router


ACTIVE_SYNC_STATES = ('queued', 'running')


def enqueue_router_sync(router, requested_by=None):
    """Queue one router if it does not already have a pending/running job."""
    existing = router.sync_jobs.filter(status__in=ACTIVE_SYNC_STATES).order_by('-created_at').first()
    if existing:
        return existing, False

    job = RouterSyncJob.objects.create(
        business=router.business,
        router=router,
        requested_by=requested_by,
        status='queued',
        progress=0,
        phase='Queued — waiting for background worker',
    )
    try:
        result = sync_router_task.delay(job.pk)
    except Exception as exc:
        job.status = 'failed'
        job.phase = 'Could not queue background job'
        job.error = str(exc)
        job.finished_at = timezone.now()
        job.save(update_fields=['status','phase','error','finished_at','updated_at'])
        raise
    job.celery_task_id = result.id
    job.save(update_fields=['celery_task_id', 'updated_at'])
    return job, True


@shared_task(bind=True, acks_late=True)
def sync_router_task(self, job_id):
    job = RouterSyncJob.objects.select_related('router', 'business').get(pk=job_id)
    router = job.router
    job.status = 'running'
    job.progress = max(job.progress, 1)
    job.phase = 'Background worker started'
    job.started_at = timezone.now()
    job.error = ''
    job.save(update_fields=['status','progress','phase','started_at','error','updated_at'])

    def progress(percent, phase):
        RouterSyncJob.objects.filter(pk=job.pk).update(
            progress=percent,
            phase=phase,
            updated_at=timezone.now(),
        )

    try:
        summary = sync_router(router, progress=progress)
        warning_count = len(summary.get('errors') or [])
        job.status = 'success'
        job.progress = 100
        job.phase = 'Completed' + (f' with {warning_count} warning(s)' if warning_count else '')
        job.summary = summary
        job.error = ''
        job.finished_at = timezone.now()
        job.save(update_fields=['status','progress','phase','summary','error','finished_at','updated_at'])
        return summary
    except Exception as exc:
        now = timezone.now()
        Router.objects.filter(pk=router.pk).update(
            status='Offline', last_error=str(exc), last_tested_at=now
        )
        job.status = 'failed'
        job.phase = 'Synchronization failed'
        job.error = str(exc)
        job.finished_at = now
        job.save(update_fields=['status','phase','error','finished_at','updated_at'])
        raise


@shared_task(ignore_result=True)
def live_watch_all():
    """Every LIVE_WATCH_SECONDS: quick voucher/session/binding pass over all routers."""
    from .live import watch_all
    watch_all()
