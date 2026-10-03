"""TapTap Link quick install for a NEW MikroTik: certificates → Link → tunnel → RouterOS update.

Most new MikroTiks trust no root certificate, so every HTTPS call to TapTap (Link check-ins, the tunnel
bootstrap) fails with "no trusted CA". The quick-install block therefore starts by fixing that:

1. **Clock and certificates** — sync the clock (HTTPS fails with a wrong date), set DNS servers when the
   router has none, then test HTTPS to TapTap. If it fails: switch on RouterOS's built-in trust store
   (RouterOS 7.19+); if it still fails, import the root certificates carried *inside the block itself*
   (core/data/link_ca_roots.pem — Let's Encrypt, Google Trust Services, Sectigo; plus AGENT_CA_PEM_FILE).
   They arrive through TapTap's own HTTPS page, so nothing is downloaded without checking.
   Works on RouterOS 6 (one certificate per file, each under 4 KB) and 7.
2. **TapTap Link** (core/agent.enrollment_script).
3. **TapTap Tunnel** (RouterOS 7), now saying clearly whether it started.
4. **RouterOS update — last**, because installing an update reboots the router: placed first, nothing after
   it would run. TapTap Link starts again by itself after the reboot. Off with LINK_INSTALL_UPDATE=False.

Each step is wrapped in ``{ … }`` so its local variables live for the whole step when pasted into a terminal.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings

ROOTS_FILE = Path(__file__).resolve().parent / 'data' / 'link_ca_roots.pem'
PEM_RE = re.compile(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', re.S)


def root_pems():
    """The root certificates carried in the quick install (vendored set + AGENT_CA_PEM_FILE)."""
    pems = PEM_RE.findall(ROOTS_FILE.read_text(encoding='utf-8'))
    extra = getattr(settings, 'AGENT_CA_PEM_FILE', '') or ''
    if extra:
        try:
            pems += [p for p in PEM_RE.findall(Path(extra).read_text(encoding='utf-8')) if p not in pems]
        except OSError:
            pass
    return [p for p in pems if len(p) < 4000]     # RouterOS 6 cannot write a longer file


def _rs(value):
    from .agent import rs
    return rs(value)


def trust_block(url, check):
    """Step 1: clock, DNS and a trusted HTTPS connection to TapTap."""
    if check == 'no':          # TapTap is not checked over HTTPS: nothing to trust
        return ''
    hello = _rs(url + '/api/agent/v1/hello')
    pems = ';'.join(_rs(p) for p in root_pems())
    return f'''
# === 1) Clock and certificates: HTTPS to TapTap needs the right date and a trusted root certificate ===
:do {{ /system ntp client set enabled=yes servers=pool.ntp.org }} on-error={{ :do {{ /system ntp client set enabled=yes server-dns-names=pool.ntp.org }} on-error={{}} }}
:do {{ :if (([:len [/ip dns get servers]] = 0) and ([:len [/ip dns get dynamic-servers]] = 0)) do={{ /ip dns set servers=1.1.1.1,8.8.8.8 }} }} on-error={{}}
{{
  :local ok false
  :delay 3s
  :do {{ /tool fetch url={hello} output=none check-certificate={check} duration=8s idle-timeout=5s; :set ok true }} on-error={{}}
  :if (!$ok) do={{
    :put "No trusted certificate yet - switching on the RouterOS trust store..."
    :do {{ /certificate settings set builtin-trust-anchors=trusted }} on-error={{}}
    :delay 2s
    :do {{ /tool fetch url={hello} output=none check-certificate={check} duration=8s idle-timeout=5s; :set ok true }} on-error={{}}
  }}
  :if (!$ok) do={{
    :put "Installing the root certificates carried in this block..."
    :local pems {{{pems}}}
    :local i 0
    :foreach pem in=$pems do={{
      :set i ($i + 1)
      :local f ("taptap-ca-" . $i . ".pem")
      :do {{ /file remove [find name=$f] }} on-error={{}}
      :do {{ /file add name=$f contents=$pem }} on-error={{
        :do {{ /file print file=("taptap-ca-" . $i); :delay 2s; :set f ("taptap-ca-" . $i . ".txt"); /file set $f contents=$pem }} on-error={{}}
      }}
      :delay 1s
      :do {{ /certificate import file-name=$f passphrase="" trusted=yes }} on-error={{ :do {{ /certificate import file-name=$f passphrase="" }} on-error={{}} }}
      :do {{ /file remove [find name=$f] }} on-error={{}}
    }}
    :delay 2s
    :do {{ /tool fetch url={hello} output=none check-certificate={check} duration=8s idle-timeout=5s; :set ok true }} on-error={{}}
  }}
  :if ($ok) do={{ :put "Step 1 OK: HTTPS to TapTap is trusted"; :log info "TapTap: HTTPS to TapTap is trusted" }} else={{
    :put "Step 1 FAILED: HTTPS to TapTap is still not trusted. Check the date (/system clock print) and DNS, then paste this block again."
    :log warning "TapTap: HTTPS to TapTap is not trusted yet"
  }}
}}
'''


def tunnel_block(token):
    """Step 3: the TapTap Tunnel (RouterOS 7), saying whether it started."""
    try:
        from .tunnel import enrollment_bootstrap_trigger, tunnel_enabled
    except Exception:
        return ''
    if not token or not tunnel_enabled():
        return ''
    trigger = enrollment_bootstrap_trigger(token).strip()
    if not trigger:
        return ''
    return f'''
# === 3) TapTap Tunnel (RouterOS 7): the fast permanent connection ===
{{
{trigger}
:if ([:tonum [:pick [/system resource get version] 0 1]] >= 7) do={{ :put "Step 3: tunnel requested - it shows as connected on the TapTap Link page within about a minute" }} else={{ :put "Step 3 skipped: the tunnel needs RouterOS 7 - TapTap Link works without it" }}
}}
'''


def update_block():
    """Step 4: install a RouterOS update if one exists — last, because it reboots the router."""
    if not getattr(settings, 'LINK_INSTALL_UPDATE', True):
        return ''
    return '''
# === 4) Update RouterOS - LAST: installing an update reboots the router ===
#     TapTap Link starts again by itself after the reboot. Afterwards you can also run: /system routerboard upgrade
{
  :do {
    /system package update set channel=stable
    /system package update check-for-updates once
    :delay 8s
    :local st [/system package update get status]
    :if ($st ~ "New version") do={
      :put ("Step 4: " . $st . " - installing; the router reboots in a moment")
      :log info "TapTap: installing a RouterOS update"
      /system package update install
    } else={ :put ("Step 4: RouterOS - " . $st) }
  } on-error={ :put "Step 4: could not check for RouterOS updates - update later from System > Packages" }
}
'''
