# TapTap update — print only what is left of a batch

No migration, no new packages. Built on `master` at `91da232`; independent of the Config Studio update.

## What you get

* **Batches list:** once part of a batch has sold, its button reads **Print 37 left** and prints only those. The arrow
  beside it still offers **Whole batch · 100** (and your designs).
* **Batch page:** **Print what's left** and **Whole batch** side by side.
* **Stock ribbon on the print page.** A bar shows the batch as *Left to sell · Sold, not used yet · Used · Switched off /
  expired*. Tap a group to put it on, or take it off, the sheets; they redraw at once. It also shows the serial ranges on
  the sheets (`001052–001059, 001062–001069 …`) and how many sheets of paper you save against the whole batch.
* **Pull list** (new layout): instead of reprinting, print one page of the codes to cross off the sheets you already
  have — sold, used and switched-off ones, each with its serial and a tick box.
* **Tally slip** (tick box, Cards layout): the first place on the print becomes a slip with the batch name, "63 of 120",
  the serial ranges, the date and lines for *Counted by* / *Received by* — for handing stock to an agent.
* The Code sheet layout gets the same "63 of 120 · serials … · printed …" line on its first sheet.

"Left" means: active, not sold, not used. Vouchers in the bin are never printed. Links without `show=` (agent pages,
old bookmarks) still open the whole batch, now with the ribbon above it. A batch page loads up to 2000 vouchers.

## Server

    cd /opt/taptap
    unzip -o ~/taptap-print-whats-left.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose up -d --force-recreate web worker vouchers live

## Files

New: `core/batch_print.py`, `core/tests/test_batch_print.py`.
Changed: `core/views_studio.py` (the `voucher_print` view only), `templates/core/studio/voucher_print.html`,
`templates/core/batches.html`, `templates/core/batch_detail.html`.

## Tests

    python manage.py test core.tests.test_batch_print      # 6
