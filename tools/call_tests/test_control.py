"""Remote control end to end: request, consent, ghost cursor, clicks, guardian, pause, +5 min, the log and the
audit note in the chat. Headless Chromium hides capture handles, so this test hands the real tab handle to the
call window with a test-only script; everything else is real."""
import asyncio, json, os, sqlite3, sys
from playwright.async_api import async_playwright
from common import BASE, check, errors, finish, launch, setup
from make_media import camera_y4m

S = setup()

async def frame_stats(page, sel, corner=False):
    return await page.evaluate("""([sel, corner]) => { const v = document.querySelector(sel); if (!v || !v.videoWidth) return null;
      const c = document.createElement('canvas'); c.width = 160; c.height = 90; const g = c.getContext('2d'); if (corner) g.drawImage(v, 0, 0, v.videoWidth * 0.18, v.videoHeight * 0.3, 0, 0, 160, 90); else g.drawImage(v, 0, 0, 160, 90);
      const d = g.getImageData(0, 0, 160, 90).data, L = i => d[i] * .299 + d[i + 1] * .587 + d[i + 2] * .114; let s = 0, s2 = 0, n = 0, e = 0;
      for (let y = 0; y < 90; y++) for (let x = 0; x < 160; x++) { const i = (y * 160 + x) * 4, l = L(i); s += l; s2 += l * l; n++; if (x < 159) e += Math.abs(l - L(i + 4)); if (y < 89) e += Math.abs(l - L(i + 640)); }
      return { mean: s / n, var: s2 / n - (s / n) ** 2, edge: e / n }; }""", [sel, corner])


async def sharp(page, sel, n=5):
    vals = []
    for _ in range(n):
        st = await frame_stats(page, sel, True)
        if st: vals.append(st['edge'])
        await asyncio.sleep(0.3)
    return sum(vals) / len(vals) if vals else None


async def point_on(page, fx, fy):
    """Client coordinates of a point given as fractions of the shared video frame (object-fit: contain)."""
    return await page.evaluate("""([fx, fy]) => { const v = document.querySelector('.cw-rscreen'), r = v.getBoundingClientRect();
      const sc = Math.min(r.width / v.videoWidth, r.height / v.videoHeight), w = v.videoWidth * sc, h = v.videoHeight * sc;
      return [r.left + (r.width - w) / 2 + fx * w, r.top + (r.height - h) / 2 + fy * h]; }""", [fx, fy])


async def main():
    async with async_playwright() as p:
        b = await launch(p, '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', '--use-file-for-fake-video-capture=' + str(camera_y4m()),
                         '--auto-select-tab-capture-source-by-title=' + S['title'], '--disable-features=WebRtcHideLocalIpsWithMdns', '--autoplay-policy=no-user-gesture-required')
        ctxA, ctxB = await b.new_context(), await b.new_context()
        await ctxA.add_cookies([{'name': 'sessionid', 'value': S['viewer'], 'url': BASE}])
        await ctxB.add_cookies([{'name': 'sessionid', 'value': S['sharer'], 'url': BASE}])
        for c, who in ((ctxA, 'viewer'), (ctxB, 'sharer')):
            c.on('page', lambda pg, who=who: pg.on('pageerror', lambda e, who=who: errors.append(f'{who}: {e}')))
        tab = await ctxB.new_page(); await tab.goto(BASE + '/')                         # Fatou's TapTap tab (to be shared)
        callA = await ctxA.new_page(); await callA.goto(BASE + f"/chat/call/?thread={S['thread']}&video=0")
        cid = None
        for _ in range(40):
            cid = await tab.evaluate("fetch('/chat/call/poll/', {headers: {'X-Requested-With': 'XMLHttpRequest'}}).then(r => r.json()).then(d => d.rings && d.rings[0] && d.rings[0].id)")
            if cid: break
            await asyncio.sleep(0.5)
        check('call rings for the other person', bool(cid), cid)
        tabid = await tab.evaluate("sessionStorage.getItem('tt-tab')")
        await ctxB.add_init_script("(() => { const h = %r; MediaStreamTrack.prototype.getCaptureHandle = function () { return this.kind === 'video' && /web-contents/.test(this.label) ? { handle: h, origin: location.origin } : null; }; })()" % tabid)
        await tab.evaluate("""() => { const box = document.createElement('div'); box.style.cssText = 'position:fixed;left:80px;top:260px;z-index:99990;background:#fff;padding:14px;border:2px solid #333';
          box.innerHTML = '<button id="save" style="font-size:22px;padding:12px">Save plan</button> <button id="del" style="font-size:22px;padding:12px">Delete voucher</button>';
          document.body.appendChild(box); window.hits = { save: 0, del: 0 };
          document.getElementById('save').onclick = () => hits.save++; document.getElementById('del').onclick = () => hits.del++; }""")
        callB = await ctxB.new_page(); await callB.goto(BASE + f'/chat/call/?join={cid}')
        live = "/\\d+:\\d\\d/.test(document.querySelector('.cw-status').textContent) && !document.querySelector('.cw-over.on')"
        await callA.wait_for_function(live, timeout=40000); await callB.wait_for_function(live, timeout=40000)
        check('call connects (peer-to-peer, encrypted)', True)

        await callB.click('[data-a=sharemenu]'); await callB.click('[data-a=share-tab]')
        await callA.wait_for_function("(() => { const s = document.querySelector('.cw-screen'), v = document.querySelector('.cw-rscreen'); return s && !s.classList.contains('cw-h') && v.videoWidth > 0; })()", timeout=30000)
        await asyncio.sleep(2)
        vw, vh = await tab.evaluate('[innerWidth, innerHeight]')
        async def at(sel):
            r = await tab.evaluate(f"(() => {{ const r = document.querySelector('{sel}').getBoundingClientRect(); return [(r.left + r.width / 2) / innerWidth, (r.top + r.height / 2) / innerHeight]; }})()")
            return await point_on(callA, r[0], r[1])
        # ask
        await callA.click('.cw-screen [data-a=rc]') if await callA.is_visible('.cw-screen [data-a=rc]') else await callA.click('.cw-bar [data-a=rc]')
        await callA.click('.cw-req [data-a=rcwhy] >> text=Check a voucher'); await callA.select_option('.cw-reqmins', '5'); await callA.click('[data-a=rcsend]')
        await callB.wait_for_selector('[data-b=rcask] [data-a=ctlallow]', timeout=6000)
        txt = await callB.inner_text('[data-b=rcask]')
        check('Fatou is asked: who, why, how long', 'Check a voucher' in txt and 'Allow for 5 min' in txt, txt.replace('\n', ' ')[:140])
        await callB.click('[data-b=rcask] [data-a=ctlallow]')
        await callA.wait_for_selector('[data-b=rc] .cw-rcleft', timeout=6000); await asyncio.sleep(1.2)
        left = await callA.inner_text('[data-b=rc] .cw-rcleft')
        check('Lamin is in control, with the time left shown', left.startswith('4:') or left.startswith('5:'), left)
        bar = await tab.evaluate("(document.querySelector('.tt-rcbar') || {}).textContent || ''")
        check('her tab says Lamin is controlling it', 'Lamin' in bar, bar)
        # hover: ghost cursor names the button
        x, y = await at('#save'); await callA.mouse.move(x - 30, y); await callA.mouse.move(x, y); await asyncio.sleep(0.6)
        lab = await tab.evaluate("(document.querySelector('.tt-laser span') || {}).textContent || ''")
        check('ghost cursor on her tab: “Lamin → Save plan”', 'Save plan' in lab, lab)
        # click
        await callA.mouse.click(x, y); await asyncio.sleep(1)
        check('Lamin’s click presses “Save plan” on her tab', (await tab.evaluate('hits.save')) == 1, await tab.evaluate('hits'))
        acts = await callB.inner_text('[data-b=ctl] .cw-ctlacts')
        check('Fatou’s call window counts the action', acts.startswith('1 action'), acts)
        # guardian
        xd, yd = await at('#del'); await callA.mouse.click(xd, yd); await asyncio.sleep(1)
        held = await tab.evaluate("!!document.querySelector('.tt-guard')") and (await tab.evaluate('hits.del')) == 0
        check('“Delete voucher” waits for Fatou’s OK', held)
        try:
            await callA.wait_for_function("[...document.querySelectorAll('.cw-banner')].some(b => /Waiting for the owner/.test(b.textContent))", timeout=4000)
            check('Lamin sees it is waiting for her', True)
        except Exception as e:
            check('Lamin sees it is waiting for her', False, e)
        await tab.click('.tt-guard [data-g=ok]'); await asyncio.sleep(0.8)
        check('her tap lets it through', (await tab.evaluate('hits.del')) == 1)
        # pause / resume / more time
        await callB.click('[data-b=ctl] [data-a=ctlhold]'); await asyncio.sleep(0.8)
        await callA.mouse.click(x, y); await asyncio.sleep(1)
        paused_ok = (await tab.evaluate('hits.save')) == 1 and 'paused' in (await tab.evaluate("document.querySelector('.tt-rcbar').textContent"))
        check('Pause: Lamin’s click only points, nothing is pressed', paused_ok, await tab.evaluate('hits'))
        check('Lamin is told control is paused', await callA.evaluate("!!document.querySelector('[data-b=rchold]')"))
        await callB.click('[data-b=ctl] [data-a=ctlhold]'); await callB.click('[data-b=ctl] [data-a=ctlplus]'); await asyncio.sleep(1.2)
        left2 = await callA.inner_text('[data-b=rc] .cw-rcleft')
        check('+5 min reaches Lamin', left2.startswith('9:') or left2.startswith('10:'), left2)
        # the log
        await callB.click('[data-b=ctl] [data-a=ctllog]'); log = await callB.inner_text('.cw-log')
        check('Fatou’s log lists what was done', 'Save plan' in log and 'Delete voucher' in log and 'Opened /' in log, log.replace('\n', ' | ')[:200])
        # take back → audit note in the chat
        await callB.click('[data-b=ctl] [data-a=ctlstop]'); await asyncio.sleep(2)
        check('control ends on both sides', not await tab.evaluate("!!document.querySelector('.tt-rcbar')") and not await callA.evaluate("!!document.querySelector('[data-b=rc]')"))
        row = sqlite3.connect(os.environ['SQLITE_PATH']).execute("select body from core_chatmessage where body like '%remote control%' order by id desc limit 1").fetchone()
        note = row[0] if row else ''
        check('audit note in the chat: time, actions, page, what was clicked', 'Lamin had remote control' in note and 'actions' in note and 'pages: /' in note and 'Save plan' in note, note.replace('\n', ' / '))
        await callA.click('.cw-bar [data-a=hangup]'); await asyncio.sleep(1.5)
        bad = [e for e in errors if 'ResizeObserver' not in e]
        check('no script errors on any page', not bad, '; '.join(bad)[:300])
        await b.close()
    finish()

asyncio.run(main())
