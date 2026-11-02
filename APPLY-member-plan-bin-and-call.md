# TapTap update — delete switched-off Member Plans to the Bin, floating Call button on the portal

Built on commit b8a2597 (1 Nov 2026). **Has a database migration (0109).**

    cd /opt/taptap
    unzip -o ~/taptap-member-plan-bin-and-call.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core
    docker compose up -d --force-recreate web worker vouchers live

## 1) Delete switched-off Member Plans — they go to the Bin
* Members → Member Plans: an inactive plan now has a **Delete** button. Active plans cannot be deleted
  (Make inactive first). A reason is required.
* A plan that still has members: the dialog asks which active Member Plan they move to. Each member keeps the
  time already used and is sent to its router again — nobody with time left is disconnected.
  Members already in the Bin do not block the delete.
* Bin → new **Member plans** tab: who, when, why, price/length/devices/speed, how many members moved and where,
  renewal receipts kept. Nothing in the Bin can be deleted again or restored; the plan's name can be reused.
* The plan's router profile (taptap-member-plan-<id>) stays on the router and shows as "plan deleted" in
  profile pickers, so nobody can choose it.
* The "members assigned" count on plan cards no longer counts members that are in the Bin.

Changed: core/models_member_plans.py, core/views_members.py, core/views_bin.py,
         templates/core/members.html, templates/core/voucher_bin.html
New:     core/member_plan_bin.py, core/migrations/0109_member_plan_bin.py, core/tests/test_member_plan_bin.py
Tests:   python manage.py test core.tests.test_member_plan_bin

## 2) Floating Call button on the customer portal
* Portal Studio → Add block → **Call button (floating)**. Switch it on/off with the eye in the block list,
  same as "Send voucher to staff"; where it sits in the list does not matter.
* Floats bottom-left or bottom-right over the whole page (outside the card, so blur/animated themes don't
  trap it). Settings: phone (blank = business phone from Settings), button text, round icon-only, pulse.
* A normal phone call (tel: link) — works on the router's own login page before the customer has internet.
  Hidden on live pages when there is no number; the preview shows a hint instead.
* Router login pages are redeployed automatically (the renderer changed); portal script versions bumped
  (renderer v7, editor v6) so phones pick up the new version.

Changed: static/studio/portal-render.js, static/studio/portal-editor.js,
         templates/core/studio/portal_public.html, portal_list.html, portal_editor.html (version only)
