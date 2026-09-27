# TapTap Tunnel - router that ALREADY has TapTap Link
# Paste this whole block in WinBox -> New Terminal. RouterOS 7 required.

:local ver [/system resource get version]
:if ([:tonum [:pick $ver 0 [:find $ver "."]]] < 7) do={ :error "TapTap Tunnel needs RouterOS 7; TapTap Link keeps working" }

:local sid [/system script find where name="taptap-link"]
:if ([:len $sid] = 0) do={ :error "TapTap Link is not installed" }

:local src [/system script get $sid source]
:local p [:find $src "ttl_"]
:local q [:find $src "\"" $p]
:local tok [:pick $src $p $q]
:if ([:len $tok] < 20) do={ :error "TapTap Link token was not found" }

:put "Requesting TapTap Tunnel configuration..."

:local r [/tool fetch \
  url="https://taptapnetwork.com/api/tunnel/v1/bootstrap" \
  http-method=post \
  http-header-field=("Authorization: Bearer " . $tok) \
  output=user as-value \
  check-certificate=yes-without-crl \
  duration=12s idle-timeout=8s]

:local code ($r->"data")
:if ([:len $code] < 20) do={ :error "TapTap returned no tunnel bootstrap" }
:execute $code

:put "TapTap Tunnel bootstrap started."
:put "Wait around 30 seconds, then run:"
:put "/interface wireguard print detail where name=\"taptap-wg\""
:put "/interface wireguard peers print detail where interface=\"taptap-wg\""
