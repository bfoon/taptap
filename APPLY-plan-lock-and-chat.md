# TapTap update — Locked Member Plans, and a chat composer that always fits

**Migration `0125_member_plan_lock`.** No new packages. Built on master (`1cd1759`).

This zip holds the **complete files** and **includes every earlier update** (Connection lab builds 2 and 3, Speed per
device). Unzip it over a server with or without them: everything is inside.

## 1. Locked Member Plans

On **Members → Member Plans**, the business owner now has a **Lock** button on every plan. It opens a small window
with two strengths:

| | **Lock** | **Seal for good** |
|---|---|---|
| Who can undo it | Only the owner (the account itself) | **Nobody — not even the owner** |
| To confirm | One click | Type the plan's name |

While a plan is locked or sealed:

* **Nobody can use it** — not the owner, not staff, not Fit, not agent free internet. It disappears from every
  "choose a plan" list (new member, change plan, move members before deleting a plan), and a gold note under the
  picker says which plans are locked, so nobody wonders where a plan went.
* **Nobody can change it** — price, speed, devices, validity, switching it off and putting it in the bin are all
  refused. The Edit / Make inactive / Delete buttons are replaced by the lock note.
* **Members already on it keep it exactly as it is** — renewals, pauses, passwords, devices and their router profile
  carry on. They cannot be moved to another plan either (that would be a change to them).
* **Only the owner can unlock** a locked plan. Staff — admins included — and platform support "viewing as" the owner
  see the lock but get no Lock / Unlock buttons, and the server refuses them if they try anyway.

What it looks like:

* The plan card turns gold (purple when sealed) with a **🔒 LOCKED** / **🛡 SEALED** tag, and a note:
  *"Locked by Awa on 17 Nov 2026 — “Old price, early members only”. Nobody can join it, leave it or change it.
  Its 12 members keep it exactly as it is."*
* The member list shows a padlock next to the plan name.
* The member's own page shows *"Gold 2024 is locked by the owner: awa.k stays on it exactly as it is"* instead of
  the Change plan form.
* Every lock, seal and unlock is written to the activity log with the reason.

The rule is enforced in the core functions **and** in the database model (`MemberPlanAssignment.save()`), so any
path — a page, Fit, the bin, an API — that tries to put someone on a locked plan or move them off it is stopped with
a plain message, never a server error.

## 2. Chat: the send button was off screen

**Cause:** a message box in a row of buttons keeps a browser-default minimum width of about 20 characters. With five
buttons beside it, the row was wider than the 380 px chat panel, so the send button was pushed out of view.

**Now:**

* **One round button at the end, always on screen.** 🎙 when the box is empty, **➤ Send** as soon as there is
  something to send (text, a photo, a shared page), a green **✓ Save** while you edit a message. It pops in when it
  changes.
* **A ＋ tray.** In a narrow chat (the side panel, any phone) Emoji, Share this page and Photo or video fold behind one
  **＋** button that opens a small labelled menu (Esc or a tap outside closes it). In the big chat window they stay
  as buttons in the row.
* The message box shrinks to fit and uses the app's font (it was showing a typewriter font). On iPhones the composer
  stays above the home bar.

Checked in a real browser at 320 px, 360 px and the desktop panel: the send button is fully inside the chat in every
state.

## Server

    cd /opt/taptap
    unzip -o ~/taptap-plan-lock-and-chat.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core          # 0122 … 0124 (if not run yet) and 0125
    docker compose up -d --force-recreate web worker vouchers live

Check it worked:

    docker compose exec web python manage.py showmigrations core | tail -4     # 0122 … 0125 all [X]

Then reload with **Ctrl+F5** (new `chat.js?v=comp1`, `chat.css?v=comp1`). Nothing changes on the routers.

Changed in this build: new `core/plan_lock.py`, `core/migrations/0125_member_plan_lock.py`,
`core/tests/test_plan_lock.py`; `core/models_member_plans.py`, `core/members.py`, `core/member_plan_bin.py`,
`core/views_members.py`, `core/views_member_detail.py`, `core/views_agent_benefits.py`, `core/fit_members.py`,
`templates/core/members.html`, `templates/core/member_detail.html`, `static/js/chat.js`, `static/css/chat.css`,
and the chat version numbers in `templates/core/base.html`, `templates/core/platform/base.html`,
`templates/core/chat_standalone.html`.

Tests: `python manage.py test core.tests.test_plan_lock` (17).
