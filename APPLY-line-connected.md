# Fix: a working line showed "Not connected" and was left out of Share the load

## What people saw
On Gateways, ether5 said **Not connected**. It had its address (192.168.2.140) and gateway
(192.168.2.1), the cable was in, and Internet paths showed traffic on it. Because TapTap thought it
was down:
- "Share the load" quietly skipped it, so Balance stayed "Single path" and ether5 got 0 % of traffic;
- customers could not be moved to it;
- "Bring online" tried to refresh it.

## Why
A DHCP line counted as connected only when its status was exactly `bound`:
- RouterOS renews a lease half-way through ("renewing..."), then asks any server ("rebinding...").
  Many modems ignore the renewal, so a working line can sit in those states for hours.
- Over TapTap Link, the synced copy can miss the status, or its /interface "running" flag can be
  older than the routes.

## The fix
- `core/wan.py`: new `dhcp_has_lease()`. It treats bound, renewing and rebinding as connected. When
  the status is missing, it uses TapTap Link's one-by-one reading, then the address.
  New `route_live_on()`: an active route through the port proves the cable and modem are up.
- `core/line_hub.py`: lines use both checks. New `lines_now()` adds the live speeds: a line that has
  its address and has traffic coming in counts as connected. It is used for the page, for every
  action (Share the load, Bring online, TapTap line) and for moving customers.
- `core/wan_doctor.py`: the Internet Doctor uses the same rule, so it no longer says "the modem is
  not giving an address" for a renewing line.
- `static/js/wan-designer.js`: the same rule on the design page.

A line with no address (searching, requesting, stopped), or one with no traffic and no active
route, still shows Not connected.

## After updating
Restart web and workers. Open Internet lines: ether5 shows **In use** or **Standing by**. Then
press **Share the load** on ether5 again. The earlier attempt left it out because it was "not
connected", so it has to be shared again before traffic splits.

## Tests
- core/tests/test_line_connected.py (new, 10 tests).
- Full suite: 957 tests. The same 38 failures as untouched 6682227 (timing and environment
  tests), and no new failures.
