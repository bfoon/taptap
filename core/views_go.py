"""Short links used everywhere a voucher code or a device MAC is shown:
/go/voucher/<code>/ and /go/device/<mac>/ open the right detail page."""
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.urls import reverse


def _b(request):
    return request.user.business


@login_required
def go_voucher(request, code):
    business = _b(request)
    code = (code or '').strip()
    from .models import Voucher, VoucherCodeAlias
    v = (Voucher.all_objects.filter(business=business, code__iexact=code).only('pk').first()
         or getattr(VoucherCodeAlias.objects.filter(business=business, code__iexact=code).select_related('voucher').first(), 'voucher', None))
    if v:
        return redirect('voucher_detail', pk=v.pk)
    messages.info(request, f'{code} is not a voucher in TapTap (it may be a member, a router user or an IP binding).')
    return redirect(f"{reverse('vouchers')}?q={code}")


def norm_mac(mac):
    m = re.sub(r'[^0-9A-Fa-f]', '', str(mac or '')).upper()
    return ':'.join(m[i:i + 2] for i in range(0, 12, 2)) if len(m) == 12 else ''


@login_required
def go_device(request, mac):
    business = _b(request)
    mac = norm_mac(mac)
    if mac:
        sigs = business.device_signatures
        d = sigs.filter(last_mac__iexact=mac).first()          # newest first
        if not d:
            # a phone's earlier (random) MACs are kept on the device too
            for row in sigs.only('pk', 'macs').iterator():
                if mac in {str(m).upper() for m in (row.macs or [])}:
                    d = row
                    break
        if d:
            return redirect('device_detail', pk=d.pk)
    messages.info(request, f'{mac or "This device"} has no detail page yet — it has not opened your login page. Showing what TapTap knows about it.')
    return redirect(f"{reverse('devices')}?q={mac}")
