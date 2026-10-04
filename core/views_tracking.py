"""Track button on plans, batches and vouchers, and Alerts › Tracking (everything you follow)."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from . import tracking
from .models_watch import Watch


def _b(request):
    return request.user.business


def _item(business, kind, oid):
    """(object, label, url) — only items of this business."""
    if kind == 'plan':
        o = business.plans.filter(pk=oid).first()
        return (o, o.name, f'/plans/{o.pk}/') if o else (None, '', '')
    if kind == 'batch':
        o = business.batches.filter(pk=oid).first()
        return (o, o.name, f'/batches/{o.pk}/') if o else (None, '', '')
    if kind == 'voucher':
        o = business.vouchers.filter(pk=oid).first()
        return (o, o.code, f'/vouchers/{o.pk}/') if o else (None, '', '')
    return None, '', ''


def _back(request, url):
    nxt = request.POST.get('next', '')
    return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else url)


@login_required
@require_POST
def watch_save(request):
    business = _b(request)
    kind = request.POST.get('kind', '')
    oid = int(request.POST['object_id']) if str(request.POST.get('object_id', '')).isdigit() else 0
    obj, label, url = _item(business, kind, oid)
    perms = getattr(request, 'tt_perms', None)
    if obj is None or (perms is not None and not ({'vouchers.view', 'vouchers.create', 'plans.manage', 'vouchers.support'} & set(perms))):
        raise Http404
    if request.POST.get('stop') == '1':
        Watch.objects.filter(user=request.user, kind=kind, object_id=oid).delete()
        messages.info(request, f'You no longer track {label}.')
        return _back(request, url)
    allowed = {k for k, _, _ in tracking.events_for(kind)}
    mode = 'some' if request.POST.get('mode') == 'some' else 'all'
    events = [e for e in request.POST.getlist('events') if e in allowed]
    if mode == 'some' and not events:
        messages.error(request, 'Choose at least one thing to be told about — or pick “Everything”.')
        return _back(request, url)
    w, created = Watch.objects.update_or_create(
        user=request.user, kind=kind, object_id=oid,
        defaults={'business': business, 'label': label[:160], 'mode': mode, 'events': events if mode == 'some' else [],
                  'channel': 'app_email' if request.POST.get('channel') == 'app_email' else 'app',
                  'note': request.POST.get('note', '').strip()[:200], 'active': request.POST.get('paused') != '1'})
    how = 'in the app and by email' if w.channel == 'app_email' else 'in the app'
    what = 'everything' if mode == 'all' else ', '.join(dict((k, l) for k, l, _ in tracking.events_for(kind))[e].lower() for e in events)
    messages.success(request, f'{"Now tracking" if created else "Tracking updated for"} {label} — {what}, {how}.'
                     + ('' if w.channel == 'app' or request.user.email else ' Add your email under My account to get the emails.'))
    return _back(request, url)


@login_required
def tracking_list(request):
    business = _b(request)
    watches = list(Watch.objects.filter(user=request.user, business=business))
    for w in watches:
        obj, label, url = _item(business, w.kind, w.object_id)
        w.url, w.gone = url, obj is None
        names = dict((k, l) for k, l, _ in tracking.events_for(w.kind))
        w.what = 'Everything' if w.mode == 'all' else ', '.join(names.get(e, e) for e in w.events)
    from .business_alerts import visible
    recent = visible(business, request.user).filter(user=request.user)[:30]
    return render(request, 'core/tracking.html', {'watches': watches, 'recent': recent})


@login_required
@require_POST
def tracking_action(request, pk):
    w = Watch.objects.filter(pk=pk, user=request.user).first()
    if not w:
        raise Http404
    if request.POST.get('action') == 'delete':
        w.delete(); messages.info(request, f'Stopped tracking {w.label}.')
    else:
        w.active = not w.active; w.save(update_fields=['active'])
        messages.info(request, f'{"Resumed" if w.active else "Paused"} tracking {w.label}.')
    return redirect('tracking')
