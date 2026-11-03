# TapTap update — floating Call button, fixed and redesigned

Builds on taptap-member-plan-bin-and-call.zip (apply that one first). No database migration.

    cd /opt/taptap
    unzip -o ~/taptap-call-button-v2.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose up -d --force-recreate web worker vouchers live

Router login pages redeploy by themselves (the renderer changed). Scripts are now renderer v8 / editor v7.

## The bug
In Portal Studio the button sat next to the card and pushed it left, and "Call us" broke onto two lines.
The Studio's "click to select a block" style (position: relative) overrode the button's fixed position.
Now the button is always pinned to the corner, never wraps, and the page gets room at the bottom so the
last lines can scroll clear of it.

## What's new
* Button: phone icon in a white bubble with two ripples and a small ring wiggle, plus an optional second line
  ("We can help"). Round icon-only style still available. Bottom-left or bottom-right.
* Tap → help card (default): your logo or initials, name, hours, the number in big type, a short message,
  a big **Call now** button and optional **WhatsApp** (marked "needs internet"). Closes with ×, a tap outside,
  or Esc. Choose "Dial at once" if you want the old one-tap behaviour.
* Hint bubble: "Need help getting online? We are one call away." pops up once per visit, 3.5 s after the page
  opens, and goes away after a few seconds. Tapping it opens the card. Blank the text to turn it off.
* The call itself is still a plain phone call, so it works on the router page before the customer is online.
* Pages that already have the Call block get the help card and hint automatically; edit the texts in the block.

Changed: static/studio/portal-render.js, static/studio/portal-editor.js,
         templates/core/studio/portal_public.html, portal_list.html, portal_editor.html (version only)
