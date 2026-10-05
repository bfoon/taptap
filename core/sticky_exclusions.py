'''Phone/model exclusions for TapTap sticky vouchers and sticky HotSpot login.

skip_device_lock:
    The matching device does not remain permanently bound to a TapTap voucher
    slot. RouterOS shared-users/concurrent limits still apply, but TapTap's
    persistent anti-sharing binding is intentionally relaxed.

skip_sticky_sessions:
    The matching device can use the current session normally, but TapTap removes
    that device's HotSpot MAC cookie after login. On its next reconnect it sees
    the portal instead of silently logging in.
'''
from __future__ import annotations

import json
import logging
import re
from functools import wraps

from celery import shared_task
from django.contrib import messages
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.db.models.signals import post_save
from django.utils.html import escape

from .models import DeviceSignature, Voucher, VoucherDeviceBinding
from .models_sticky_exclusions import StickyExclusionRule

logger = logging.getLogger("taptap.sticky_exclusions")

MAX_RULES = 20
VALID_FIELDS = {"model", "os", "device_type", "browser"}
_ORIGINAL_CLAIM = None
_INSTALLED = False


def _norm(value):
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def clean_rules(raw):
    '''Validate UI JSON and de-duplicate rules case-insensitively.'''
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except ValueError as exc:
            raise ValueError("Sticky exclusion settings are not valid JSON.") from exc
    if not isinstance(raw, list):
        raise ValueError("Sticky exclusions must be a list.")
    if len(raw) > MAX_RULES:
        raise ValueError(f"You can configure up to {MAX_RULES} sticky exclusion rules.")

    out, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        field = str(item.get("match_field") or "model").strip()
        if field not in VALID_FIELDS:
            raise ValueError("Choose a valid sticky exclusion match field.")
        value = re.sub(r"\s+", " ", str(item.get("value") or "").strip())[:120]
        if not value:
            continue
        skip_lock = _bool(item.get("skip_device_lock"))
        skip_sticky = _bool(item.get("skip_sticky_sessions"), True)
        if not skip_lock and not skip_sticky:
            raise ValueError(
                f'Rule "{value}" does not exclude anything. Select device lock, '
                "automatic sticky login, or both."
            )
        key = (field, _norm(value))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "match_field": field,
                "value": value,
                "skip_device_lock": skip_lock,
                "skip_sticky_sessions": skip_sticky,
                "enabled": _bool(item.get("enabled"), True),
            }
        )
    return out


@transaction.atomic
def save_rules(business, raw):
    rules = clean_rules(raw)
    keep = []
    for item in rules:
        row = business.sticky_exclusion_rules.filter(
            match_field=item["match_field"],
            value__iexact=item["value"],
        ).first()
        if row is None:
            row = StickyExclusionRule(
                business=business,
                match_field=item["match_field"],
                value=item["value"],
            )
        row.value = item["value"]
        row.skip_device_lock = item["skip_device_lock"]
        row.skip_sticky_sessions = item["skip_sticky_sessions"]
        row.enabled = item["enabled"]
        row.save()
        keep.append(row.pk)

    business.sticky_exclusion_rules.exclude(pk__in=keep).delete()
    return len(keep)


def rule_matches(rule, signature):
    expected = _norm(rule.value)
    actual = _norm(getattr(signature, rule.match_field, ""))
    if not expected or not actual:
        return False
    if rule.match_field == "device_type":
        return actual == expected
    return expected in actual


def matching_rules(signature, purpose=None):
    qs = signature.business.sticky_exclusion_rules.filter(enabled=True)
    if purpose == "device_lock":
        qs = qs.filter(skip_device_lock=True)
    elif purpose == "sticky_sessions":
        qs = qs.filter(skip_sticky_sessions=True)
    return [rule for rule in qs if rule_matches(rule, signature)]


def _signature_for(voucher, fp="", mac=""):
    from .device_lock import norm_mac

    fp = re.sub(r"[^0-9a-f]", "", str(fp or "").lower())[:64]
    mac = norm_mac(mac)
    qs = voucher.business.device_signatures.all()

    if fp:
        hit = qs.filter(fingerprint=fp).first()
        if hit:
            return hit

    if mac:
        hit = qs.filter(last_mac__iexact=mac).order_by("-last_seen").first()
        if hit:
            return hit
        # Portable fallback for DEV SQLite and PostgreSQL: JSON containment
        # support differs, so only inspect a bounded list if last_mac misses.
        for sig in qs.exclude(macs=[]).order_by("-last_seen")[:200]:
            if mac in {norm_mac(x) for x in (sig.macs or [])}:
                return sig
    return None


def excluded(voucher, fp="", mac="", purpose="device_lock"):
    sig = _signature_for(voucher, fp=fp, mac=mac)
    if not sig:
        return False, None, []
    rules = matching_rules(sig, purpose)
    return bool(rules), sig, rules


def _protected_claim(voucher, mac="", fp="", source="portal", label="", hints=None):
    skip, sig, rules = excluded(
        voucher,
        fp=fp,
        mac=mac,
        purpose="device_lock",
    )
    if skip:
        from .device_lock import Outcome

        names = ", ".join(r.value for r in rules[:3])
        return Outcome(
            "off",
            message=(
                f"This device ({sig.model or sig.os or sig.device_type}) matches "
                f"a TapTap sticky exclusion ({names})."
            ),
        )
    return _ORIGINAL_CLAIM(
        voucher,
        mac=mac,
        fp=fp,
        source=source,
        label=label,
        hints=hints,
    )


def _release_matching_bindings(signature):
    from .device_lock import norm_mac

    fp = (signature.fingerprint or "").strip()
    macs = {norm_mac(m) for m in (signature.macs or []) if norm_mac(m)}
    last = norm_mac(signature.last_mac)
    if last:
        macs.add(last)

    q = Q()
    if fp:
        q |= Q(device_token_hash=fp)
    if macs:
        q |= Q(current_mac__in=macs) | Q(previous_mac__in=macs)
    if not q:
        return 0

    rows = list(
        VoucherDeviceBinding.objects.filter(
            business=signature.business,
        )
        .filter(q)
        .select_related("voucher")
    )
    if not rows:
        return 0

    try:
        from .voucher_history import record

        for binding in rows:
            record(
                binding.voucher,
                "device",
                source="auto",
                kind="sticky_exclusion",
                text=(
                    f"Sticky exclusion released device slot {binding.slot_no}: "
                    f"{signature.model or signature.os or signature.device_type or 'matched device'}"
                ),
            )
    except Exception:
        logger.exception("Could not write sticky-exclusion voucher history")

    pks = [row.pk for row in rows]
    VoucherDeviceBinding.objects.filter(pk__in=pks).delete()
    return len(pks)


def _vouchers_for_signature(signature):
    seen, out = set(), []
    for value in signature.vouchers or []:
        code = str(value or "").strip()
        key = code.lower()
        if not code or key in seen:
            continue
        seen.add(key)
        voucher = (
            signature.business.vouchers.filter(code__iexact=code)
            .select_related("router")
            .first()
        )
        if voucher:
            out.append(voucher)
        if len(out) >= 30:
            break
    return out


def _schedule_cookie_clears(signature):
    from .device_lock import norm_mac

    macs = []
    for value in [signature.last_mac] + list(signature.macs or []):
        mac = norm_mac(value)
        if mac and mac not in macs:
            macs.append(mac)
    if not macs:
        return 0

    jobs = []
    for voucher in _vouchers_for_signature(signature):
        if not voucher.router_id:
            continue
        for mac in macs[:4]:
            key = f"sticky-ex-cookie:{voucher.pk}:{mac}"
            try:
                if not cache.add(key, 1, 45):
                    continue
            except Exception:
                pass
            jobs.append((voucher.pk, mac))

    if not jobs:
        return 0

    def queue():
        for voucher_id, mac in jobs:
            # Portal signature capture usually happens just before RouterOS
            # completes authentication. Repeating briefly ensures the newly
            # created MAC cookie is removed without dropping the active session.
            for seconds in (6, 20, 45):
                try:
                    clear_sticky_cookie.apply_async(
                        args=[voucher_id, mac],
                        countdown=seconds,
                    )
                except Exception:
                    logger.exception(
                        "Could not queue sticky cookie clear for voucher %s",
                        voucher_id,
                    )
                    break

    transaction.on_commit(queue)
    return len(jobs)


def enforce_signature(signature):
    lock_rules = matching_rules(signature, "device_lock")
    sticky_rules = matching_rules(signature, "sticky_sessions")
    released = _release_matching_bindings(signature) if lock_rules else 0
    cookies = _schedule_cookie_clears(signature) if sticky_rules else 0
    return released, cookies


def _signature_saved(sender, instance, **kwargs):
    key = f"sticky-ex-sig:{instance.pk}"
    try:
        if not cache.add(key, 1, 8):
            return
    except Exception:
        pass
    try:
        enforce_signature(instance)
    except Exception:
        logger.exception("Sticky exclusion enforcement failed for signature %s", instance.pk)


def apply_known_exclusions(business):
    released = cookie_targets = matched = 0
    for sig in business.device_signatures.all().order_by("-last_seen")[:5000]:
        if matching_rules(sig):
            matched += 1
            r, c = enforce_signature(sig)
            released += r
            cookie_targets += c
    return {
        "matched_devices": matched,
        "released_bindings": released,
        "cookie_targets": cookie_targets,
    }


@shared_task(name="core.sticky_exclusion_cookie_clear")
def clear_sticky_cookie(voucher_id, mac):
    '''Remove only this user's cookie for this MAC; leave active session online.'''
    from .device_lock import norm_mac
    from .voucher_history import channel

    mac = norm_mac(mac)
    voucher = Voucher.objects.select_related("router").filter(pk=voucher_id).first()
    if not voucher or not voucher.router_id or not mac:
        return "nothing to do"

    if channel(voucher.router) == "TapTap Link":
        from .linkops import send

        try:
            send(
                voucher.router,
                "hotspot_cookie_remove",
                {"user": voucher.code, "mac": mac},
                label=f"No sticky login for {voucher.code} {mac}",
                minutes=10,
            )
            return "queued through TapTap Link"
        except Exception as exc:
            logger.warning("Sticky cookie Link clear failed: %s", exc)
            return f"link failed: {exc}"

    from .mikrotik import MikroTikService

    try:
        svc = MikroTikService(voucher.router).connect()
        try:
            cookies = svc.resource("/ip/hotspot/cookie")
            removed = 0
            for row in cookies.get(user=voucher.code):
                row_mac = norm_mac(
                    row.get("mac-address") or row.get("mac_address") or ""
                )
                if row_mac == mac and row.get("id"):
                    cookies.remove(id=row["id"])
                    removed += 1
            return f"removed {removed} cookie(s)"
        finally:
            svc.close()
    except Exception as exc:
        logger.warning(
            "Sticky cookie API clear failed for %s %s: %s",
            voucher.code,
            mac,
            exc,
        )
        return f"api failed: {exc}"


def _install_agent_command():
    from . import agent

    agent.SAFE_KINDS.add("hotspot_cookie_remove")
    agent.DEFAULT_EXPIRY["hotspot_cookie_remove"] = 10
    if "hotspot_cookie_remove" not in agent.VOUCHER_KINDS:
        agent.VOUCHER_KINDS = agent.VOUCHER_KINDS + ("hotspot_cookie_remove",)

    original = agent.command_body
    if getattr(original, "_sticky_exclusions_installed", False):
        return

    @wraps(original)
    def command_body(cmd):
        if cmd.kind == "hotspot_cookie_remove":
            from .device_lock import norm_mac

            params = cmd.params or {}
            user = str(params.get("user") or "")
            mac = norm_mac(params.get("mac"))
            if not user or not mac or len(user) > 120:
                raise ValueError("Invalid user/MAC for hotspot cookie removal.")
            u, m = agent.rs(user), agent.rs(mac)
            return (
                f":local u {u}; :local m {m}; "
                ':foreach c in=[/ip hotspot cookie find where user=$u] do={ '
                ':local cm [/ip hotspot cookie get $c mac-address]; '
                ':if ($cm = $m) do={ '
                ':do { /ip hotspot cookie remove $c } on-error={} '
                "} }"
            )
        return original(cmd)

    command_body._sticky_exclusions_installed = True
    agent.command_body = command_body


def _rule_row(rule=None):
    field = rule.match_field if rule else "model"
    value = escape(rule.value if rule else "iPhone")
    lock = bool(rule.skip_device_lock) if rule else False
    sticky = bool(rule.skip_sticky_sessions) if rule else True
    enabled = bool(rule.enabled) if rule else True

    options = []
    for val, label in StickyExclusionRule.MATCH_FIELDS:
        selected = " selected" if val == field else ""
        options.append(
            f'<option value="{val}"{selected}>{escape(label)}</option>'
        )

    return f'''
    <div class="sticky-ex-row" data-sticky-rule>
      <div>
        <label>Match by</label>
        <select class="form-select sx-field">{''.join(options)}</select>
      </div>
      <div>
        <label>Value</label>
        <input class="form-control sx-value" value="{value}" placeholder="e.g. iPhone">
      </div>
      <div class="sticky-effects">
        <label><input class="form-check-input me-1 sx-lock" type="checkbox"{' checked' if lock else ''}> Skip voucher device lock</label>
        <label><input class="form-check-input me-1 sx-sticky" type="checkbox"{' checked' if sticky else ''}> Skip automatic sticky login</label>
        <label><input class="form-check-input me-1 sx-enabled" type="checkbox"{' checked' if enabled else ''}> Enabled</label>
      </div>
      <button type="button" class="btn btn-outline-danger sx-remove" title="Remove rule"><i class="bi bi-trash"></i></button>
    </div>'''


def _settings_markup(business):
    rules = list(business.sticky_exclusion_rules.all())
    rows = "".join(_rule_row(r) for r in rules)
    if not rows:
        rows = (
            '<div class="sticky-ex-empty" id="stickyExEmpty">'
            '<i class="bi bi-phone fs-4 d-block mb-1"></i>'
            "No phone/model exclusions. Sticky behaviour applies normally to every device."
            "</div>"
        )

    return f'''
    <div class="sticky-exclusions mt-4" id="ttStickyExclusions">
      <input type="hidden" name="sticky_exclusions_json" id="stickyExclusionsJson" value="[]">
      <div class="d-flex justify-content-between gap-2 align-items-start flex-wrap mb-2">
        <div>
          <b class="small">Phone / device exclusions</b>
          <div class="form-text">Choose models or platforms that should behave differently from normal sticky vouchers.</div>
        </div>
        <button type="button" class="btn btn-sm btn-outline-primary" id="addStickyExclusion"><i class="bi bi-plus-lg"></i> Add exclusion</button>
      </div>
      <div class="sticky-ex-list" id="stickyExclusionList">{rows}</div>
      <div class="sticky-examples mt-2">
        <span>iPhone</span><span>iPad</span><span>iOS</span><span>Samsung</span><span>SM-A155F</span><span>phone</span>
        <small class="ms-auto"><span id="stickyExCount">{len(rules)}</span> / {MAX_RULES}</small>
      </div>
      <div class="alert alert-warning py-2 px-3 mt-3 mb-0 small">
        <b>Important:</b> “Skip voucher device lock” deliberately stops matching phones from permanently occupying a TapTap voucher slot.
        RouterOS concurrent-device limits still apply, but persistent anti-sharing protection is reduced for that model.
        If you only want an iPhone to see the login page again after it disconnects, select <b>Skip automatic sticky login</b> only.
      </div>
    </div>'''


SETTINGS_CSS = r'''
<style id="tt-sticky-exclusions-css">
.sticky-exclusions{border-top:1px solid var(--border);padding-top:1rem}
.sticky-ex-list{display:grid;gap:.6rem}
.sticky-ex-row{display:grid;grid-template-columns:175px minmax(150px,1fr) minmax(270px,1.35fr) auto;gap:.55rem;align-items:end;border:1px solid var(--border);border-radius:14px;padding:.75rem;background:#fbfcfe}
.sticky-ex-row label{font-size:.74rem!important;margin:0}
.sticky-effects{min-height:38px;display:flex;align-items:center;gap:.8rem;flex-wrap:wrap}
.sticky-effects label{white-space:nowrap;font-weight:700!important}
.sticky-ex-empty{border:1px dashed #cbd6e2;border-radius:14px;padding:1.05rem;text-align:center;color:var(--muted,#748396);font-size:.8rem;background:#fbfcfe}
.sticky-examples{display:flex;gap:.35rem;flex-wrap:wrap;align-items:center}
.sticky-examples span:not(#stickyExCount){font-size:.69rem;padding:.24rem .48rem;border-radius:999px;background:#eef4ff;color:#45617e}
.sticky-examples small{color:var(--muted,#748396);font-weight:700}
@media(max-width:900px){.sticky-ex-row{grid-template-columns:1fr 1fr}.sticky-effects{grid-column:1/-1}.sx-remove{width:max-content}}
@media(max-width:600px){.sticky-ex-row{grid-template-columns:1fr}.sticky-effects{grid-column:auto}.sx-remove{width:100%}}
</style>
'''

SETTINGS_JS = r'''
<script id="tt-sticky-exclusions-js">
(function(){
  const list=document.getElementById('stickyExclusionList');
  const add=document.getElementById('addStickyExclusion');
  const count=document.getElementById('stickyExCount');
  const jsonField=document.getElementById('stickyExclusionsJson');
  const form=document.getElementById('mainSettingsForm');
  if(!list||!jsonField||!form)return;

  function rows(){return Array.from(list.querySelectorAll('[data-sticky-rule]'));}
  function options(){
    return '<option value="model">Phone / device model</option>'+
           '<option value="os">Operating system</option>'+
           '<option value="device_type">Device type</option>'+
           '<option value="browser">Browser</option>';
  }
  function update(){
    if(rows().length){const e=document.getElementById('stickyExEmpty');if(e)e.remove();}
    else if(!document.getElementById('stickyExEmpty')){
      const e=document.createElement('div');e.className='sticky-ex-empty';e.id='stickyExEmpty';
      e.innerHTML='<i class="bi bi-phone fs-4 d-block mb-1"></i>No phone/model exclusions. Sticky behaviour applies normally to every device.';
      list.appendChild(e);
    }
    if(count)count.textContent=rows().length;
    if(add)add.disabled=rows().length>=20;
  }
  function newRow(){
    const r=document.createElement('div');r.className='sticky-ex-row';r.setAttribute('data-sticky-rule','');
    r.innerHTML='<div><label>Match by</label><select class="form-select sx-field">'+options()+'</select></div>'+
      '<div><label>Value</label><input class="form-control sx-value" value="iPhone" placeholder="e.g. iPhone"></div>'+
      '<div class="sticky-effects">'+
      '<label><input class="form-check-input me-1 sx-lock" type="checkbox"> Skip voucher device lock</label>'+
      '<label><input class="form-check-input me-1 sx-sticky" type="checkbox" checked> Skip automatic sticky login</label>'+
      '<label><input class="form-check-input me-1 sx-enabled" type="checkbox" checked> Enabled</label></div>'+
      '<button type="button" class="btn btn-outline-danger sx-remove" title="Remove rule"><i class="bi bi-trash"></i></button>';
    return r;
  }
  if(add)add.addEventListener('click',function(){
    if(rows().length>=20)return;
    const e=document.getElementById('stickyExEmpty');if(e)e.remove();
    const r=newRow();list.appendChild(r);r.querySelector('.sx-value').focus();update();
  });
  list.addEventListener('click',function(ev){
    const b=ev.target.closest('.sx-remove');if(!b)return;
    const r=b.closest('[data-sticky-rule]');if(r)r.remove();update();
  });
  function serialize(){
    jsonField.value=JSON.stringify(rows().map(function(r){
      return {
        match_field:r.querySelector('.sx-field').value,
        value:(r.querySelector('.sx-value').value||'').trim(),
        skip_device_lock:r.querySelector('.sx-lock').checked,
        skip_sticky_sessions:r.querySelector('.sx-sticky').checked,
        enabled:r.querySelector('.sx-enabled').checked
      };
    }).filter(function(x){return x.value;}));
  }
  form.addEventListener('submit',serialize);
  update();
})();
</script>
'''


def _inject_settings_ui(request, response):
    if request.path.rstrip("/") != "/settings":
        return response
    if getattr(response, "streaming", False) or response.status_code != 200:
        return response
    if "text/html" not in response.get("Content-Type", "").lower():
        return response

    business = getattr(request, "tt_business", None)
    if business is None:
        business = getattr(getattr(request, "user", None), "business", None)
    if not business:
        return response

    body = response.content.decode(response.charset or "utf-8")
    if 'id="ttStickyExclusions"' in body:
        return response

    marker = '<div class="settings-actions"><span class="hint">Save updates TapTap only.'
    if marker in body:
        body = body.replace(marker, _settings_markup(business) + marker, 1)
    else:
        serial_marker = '<section class="settings-card" id="serials">'
        if serial_marker not in body:
            return response
        body = body.replace(
            serial_marker,
            '<section class="settings-card"><div class="settings-body">'
            + _settings_markup(business)
            + "</div></section>"
            + serial_marker,
            1,
        )

    body = body.replace("</head>", SETTINGS_CSS + "</head>", 1)
    body = body.replace("</body>", SETTINGS_JS + "</body>", 1)
    output = body.encode(response.charset or "utf-8")
    response.content = output
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(output))
    return response


def _install_settings_ui():
    from .team import TeamAccessMiddleware

    original = TeamAccessMiddleware.__call__
    if getattr(original, "_sticky_exclusion_ui_installed", False):
        return

    @wraps(original)
    def with_sticky_exclusions(self, request):
        response = original(self, request)

        if (
            request.method == "POST"
            and request.path.rstrip("/") == "/settings"
            and "sticky_exclusions_json" in request.POST
        ):
            business = getattr(request, "tt_business", None)
            if business is None:
                business = getattr(getattr(request, "user", None), "business", None)
            if business:
                try:
                    count = save_rules(
                        business,
                        request.POST.get("sticky_exclusions_json", "[]"),
                    )
                    if request.POST.get("sticky_apply"):
                        result = apply_known_exclusions(business)
                        messages.success(
                            request,
                            (
                                f"Saved {count} sticky exclusion rule(s). Applied to "
                                f"{result['matched_devices']} known matching device(s); "
                                f"released {result['released_bindings']} TapTap binding(s)."
                            ),
                        )
                    else:
                        messages.success(
                            request,
                            f"Saved {count} sticky exclusion rule(s).",
                        )
                except ValueError as exc:
                    messages.error(request, str(exc))
                except Exception as exc:
                    logger.exception("Could not save sticky exclusion settings")
                    messages.error(
                        request,
                        f"Sticky exclusions could not be saved: {str(exc)[:180]}",
                    )

        return _inject_settings_ui(request, response)

    with_sticky_exclusions._sticky_exclusion_ui_installed = True
    TeamAccessMiddleware.__call__ = with_sticky_exclusions


def _install_device_lock_guard():
    global _ORIGINAL_CLAIM
    from . import device_lock

    if getattr(device_lock.claim, "_sticky_exclusions_installed", False):
        return
    _ORIGINAL_CLAIM = device_lock.claim
    _protected_claim._sticky_exclusions_installed = True
    device_lock.claim = _protected_claim


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _install_agent_command()
    _install_device_lock_guard()
    _install_settings_ui()
    post_save.connect(
        _signature_saved,
        sender=DeviceSignature,
        dispatch_uid="taptap-sticky-exclusion-signature",
        weak=False,
    )
    _INSTALLED = True
