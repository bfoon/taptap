# Fix: an internet line stuck on "renewing..." for hours, even after a reboot

## What people saw
ether5 stayed on **renewing...**. It had its address, but TapTap treated it as not connected and it
carried 0 % of the traffic. Rebooting did not help.

## Why
A lease is first obtained with broadcasts, which skip the firewall. Half-way through the lease,
RouterOS renews it with an ordinary unicast packet to the modem (udp 68 → 67). That packet goes
through every firewall table, and on a router with two lines it got lost:
- **mangle**: a line mark gives the renewal a routing table that can send it out of the *other*
  line. That mark can be TapTap's own or a hand-made one such as `starlink2`.
- **nat**: the router's own renewal can be masqueraded behind the other line's address.
- **filter**: the modem's unicast answer can meet "drop everything from WAN".

The Internet Doctor on the router made it permanent. Its script accepted only `bound`, so it called
a renewing line "unbound" and switched its routes off. With its own routes off, the line's table
sent the renewals out of ether1, where nobody answers them. So the line could never come back, and
a reboot only restarted the same loop.

## The fix
1. **core/dhcp_guard.py** (new). It adds four rules at the top of their tables, for the router's
   own DHCP client traffic only:
   - mangle prerouting `udp 67→68 accept`, so the modems' answers get no line mark;
   - mangle output `udp 68→67 accept`, so renewals use the port's own connected route;
   - nat srcnat `udp 68→67 src-address-type=local accept`, so renewals are never NATed;
   - filter input `udp 67→68 accept`, so the modems' answers get in.

   It also drops old tracked DHCP connections and renews any client that is stuck on renewing or
   rebinding. The rows are commented `TapTap DHCP`, and nothing else is touched.

   It is applied:
   - at once, when the Internet lines page sees a line stuck on renewing (at most once an hour);
   - on every multi-line Apply or Share the load;
   - once to every router whose lines TapTap runs, by housekeeping.

   It uses a TapTap Link command or the direct API.
2. **Internet Doctor v4** (core/wan_doctor.py). renewing and rebinding count as having an address,
   so the line is measured and no longer taken out of use. Routers that have the Doctor upgrade
   themselves, as with v3.
3. **Line Hub**: a "renewing" badge on the line card, with an explanation.

## After updating
Restart web and workers, then open Internet lines. The guard is queued straight away. Within a
minute or two ether5 should show **bound**. Then press **Share the load** once more.

To fix the router by hand right now, paste this into its Terminal:

    /ip firewall mangle add chain=prerouting protocol=udp src-port=67 dst-port=68 action=accept comment="TapTap DHCP: answers from the modems keep no line mark" place-before=0
    /ip firewall mangle add chain=output protocol=udp src-port=68 dst-port=67 action=accept comment="TapTap DHCP: renewals leave through their own port" place-before=0
    /ip firewall nat add chain=srcnat protocol=udp src-port=68 dst-port=67 src-address-type=local action=accept comment="TapTap DHCP: renewals keep their own address" place-before=0
    /ip firewall filter add chain=input protocol=udp src-port=67 dst-port=68 action=accept comment="TapTap DHCP: answers from the modems get in" place-before=0
    /ip firewall connection remove [find where protocol="udp" and dst-address~":67\$"]
    /ip route set [find where comment~"~doctor-off:ether5"] disabled=no
    /ip dhcp-client renew [find where interface=ether5]

If the filter command complains about the dynamic fasttrack counter rule, use `place-before=1`.
The route command puts back ether5's routes that the old Doctor switched off. Doctor v4 keeps
them on.

## Tests
- core/tests/test_line_connected.py now has 17 tests.
- The Doctor version tests were updated to v4.
- Full suite: 964 tests. The same 38 failures as the baseline, and no new failures.
