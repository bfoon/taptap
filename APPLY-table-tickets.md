# TapTap update: print a Fit voucher table as tickets

**No migration.** No new packages. Built on master **39d0766**. The zip holds the **complete files** that change, in
their folders.

## What it does

Any Fit table of **vouchers** prints as tickets, using your own voucher designs (Voucher Design Studio).

* **On the table page** (`/fit/tables/<id>/`), a new **🖨 Print as tickets (N)** button opens a menu:
  * **Everything in this table** — exactly what the table shows now: its saved filters, or the filters you just
    changed, in the table's sort order. Example: *oldest first*.
  * **Only what is left to sell** — the same table cut down to the vouchers in stock.
  * **Choose the vouchers to print…** — a tick box appears on every row. Tick on any page; your choice is kept while
    you page through. Then press **Print the chosen ones**. *Tick all on this page* is in the header.
* **On the print page** you get everything that "Print what's left" has for batches:
  * the design and paper pickers
  * the stock ribbon: tick *Left to sell / Sold, not used / Used / Switched off*, with *Only what is left* and
    *Whole table*
  * the serial ranges on the sheets, a tally slip, and the **pull list** to cross off old sheets
* The print page also shows which **table filters** it printed. **Members are left out** (they sign in with a
  username and password, not a ticket), and the page says how many.
* **In Fit's chat**, every voucher table card now has **🖨 Print N as tickets** under it, next to *Open the table*
  and *CSV*.
* **Ask Fit:** *"print the unsold Daily vouchers older than a week"*. Fit builds exactly that table and gives you
  **Print as tickets**.
* **Phone app:** a print button on the table screen and on the table card in Fit's chat. It opens the ticket page in
  the browser.

Printing needs the *create vouchers* right, the same as Batches › Print. Sales, routers and reports tables have no
print button.

## Server

    cd /opt/taptap
    unzip -o ~/taptap-table-tickets.zip -d /tmp && cp -r /tmp/taptap/. /opt/taptap/
    docker compose build web worker vouchers live
    docker compose up -d --force-recreate web worker vouchers live

Reload TapTap with **Ctrl+F5** (`fit.js?v=tables2`, `fit.css?v=tables2`).

## Tests

    python manage.py test core.tests.test_fit_tables

Added `test_voucher_table_prints_as_tickets`:

* Fit's result carries the print link.
* The print page prints the table's vouchers in its order, with the stock ribbon set to "whole table".
* The page's live filters travel to the printer, and members are left out (and counted).
* The table page offers "only what is left" and choosing by hand.
* A sales table has no print button, and its print address is refused.

Full suite: 889 tests. The only failures are the same 38 that fail on master (they need a Redis cache).

## Files

Changed: `core/views_studio.py`, `core/views_fit.py`, `core/fit_tables.py`, `core/fit_tools.py`, `core/fit_ai.py`,
`core/tests/test_fit_tables.py`, `templates/core/fit_table.html`, `templates/core/studio/voucher_print.html`,
`templates/core/base.html`, `static/js/fit.js`, `static/css/fit.css`, `mobile/lib/screens/fit_workspace_screen.dart`
