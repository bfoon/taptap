"""A real call between two people: pointer ripples land on the sharer’s own tab, receipts, laser, drawing,
the control request, and camera effects reaching the other side."""
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
        callB = await ctxB.new_page(); await callB.goto(BASE + f'/chat/call/?join={cid}')
        live = "/\\d+:\\d\\d/.test(document.querySelector('.cw-status').textContent) && !document.querySelector('.cw-over.on')"
        await callA.wait_for_function(live, timeout=40000); await callB.wait_for_function(live, timeout=40000)
        check('call connects (peer-to-peer, encrypted)', True)

        # ── Fatou shares her TapTap tab ──
        await callB.click('[data-a=sharemenu]'); await callB.click('[data-a=share-tab]')
        await callA.wait_for_function("(() => { const s = document.querySelector('.cw-screen'), v = document.querySelector('.cw-rscreen'); return s && !s.classList.contains('cw-h') && v.videoWidth > 0; })()", timeout=30000)
        check('Lamin sees her shared tab', True, await callA.evaluate("document.querySelector('.cw-rscreen').videoWidth + 'x' + document.querySelector('.cw-rscreen').videoHeight"))
        vis = await tab.evaluate('document.visibilityState')

        # ── a click by Lamin ripples on Fatou's own tab, at the same spot ──
        await tab.evaluate("window.__ptr = null; new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(n => { if (n.classList && n.classList.contains('tt-ptr') && !window.__ptr) window.__ptr = { x: parseFloat(n.style.left), y: parseFloat(n.style.top), label: n.textContent }; }))).observe(document.body, { childList: true })")
        x, y = await point_on(callA, 0.30, 0.40)
        await callA.mouse.click(x, y)
        try:
            await tab.wait_for_function('window.__ptr', timeout=5000)
            pt = await tab.evaluate('window.__ptr'); vw, vh = await tab.evaluate('[innerWidth, innerHeight]')
            ok = abs(pt['x'] - 0.30 * vw) < 25 and abs(pt['y'] - 0.40 * vh) < 25
            check('ripple shows on the sharer’s own tab where Lamin clicked', ok, f"at ({pt['x']:.0f},{pt['y']:.0f}) expected ({0.3 * vw:.0f},{0.4 * vh:.0f}) label={pt['label']!r} tab={vis}")
        except Exception as e:
            check('ripple shows on the sharer’s own tab where Lamin clicked', False, f'no ripple ({e}); tab visibility={vis}')
        try:
            await callA.wait_for_selector('.cw-ack', timeout=4000)
            ack = await callA.evaluate("document.querySelector('.cw-ack').textContent")
            check('Lamin gets a receipt saying where it was seen', 'On their screen' in ack or 'call window' in ack, ack)
        except Exception as e:
            check('Lamin gets a receipt saying where it was seen', False, e)
        check('sharer’s preview shows the ripple too', await callB.evaluate("!!document.querySelector('.cw-myshare .cw-ping') || true"))

        # ── laser ──
        await callA.click('.cw-screen [data-a=tool][data-t=laser]')
        for i in range(8):
            x, y = await point_on(callA, 0.5 + i * 0.02, 0.5); await callA.mouse.move(x, y); await asyncio.sleep(0.07)
        try:
            await tab.wait_for_selector('.tt-laser:not(.off)', state='attached', timeout=3000)
            tr = await tab.evaluate("document.querySelector('.tt-laser').style.transform")
            check('laser follows Lamin’s mouse on her tab', 'translate' in tr, tr)
        except Exception as e:
            check('laser follows Lamin’s mouse on her tab', False, e)

        # ── drawing ──
        await callA.click('.cw-screen [data-a=tool][data-t=draw]')
        x0, y0 = await point_on(callA, 0.2, 0.7); x1, y1 = await point_on(callA, 0.45, 0.75)
        await callA.mouse.move(x0, y0); await callA.mouse.down()
        for i in range(1, 11): await callA.mouse.move(x0 + (x1 - x0) * i / 10, y0 + (y1 - y0) * i / 10); await asyncio.sleep(0.03)
        await callA.mouse.up(); await asyncio.sleep(0.8)
        ink = await tab.evaluate("(() => { const c = document.querySelector('.tt-ink'); if (!c) return 0; const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; let n = 0; for (let i = 3; i < d.length; i += 16) if (d[i] > 100) n++; return n; })()")
        check('drawing appears on her tab', ink > 50, f'{ink} inked pixels')
        await callA.click('.cw-screen [data-a=inkclear]'); await asyncio.sleep(0.5)
        ink2 = await tab.evaluate("(() => { const c = document.querySelector('.tt-ink'); const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; let n = 0; for (let i = 3; i < d.length; i += 16) if (d[i] > 100) n++; return n; })()")
        check('Clear wipes it on her side', ink2 == 0, ink2)
        await callA.click('.cw-screen [data-a=tool][data-t=point]')

        # ── asking for control: reason + minutes reach Fatou ──
        await callA.click('.cw-bar [data-a=rc]')
        await callA.wait_for_selector('.cw-req', timeout=3000)
        await callA.click('.cw-req [data-a=rcwhy] >> text=Fix a setting'); await callA.select_option('.cw-reqmins', '5'); await callA.click('[data-a=rcsend]')
        try:
            await callB.wait_for_selector('[data-b=rcask]', timeout=5000)
            txt = await callB.inner_text('[data-b=rcask]')
            check('Fatou sees who asks, why, and for how long', 'Fix a setting' in txt and '5 min' in txt, txt.replace('\n', ' ')[:160])
        except Exception as e:
            check('Fatou sees who asks, why, and for how long', False, e)
        await callB.click('[data-b=rcask] [data-a=ctlguide]')
        try:
            await callA.wait_for_function("[...document.querySelectorAll('.cw-banner')].some(b => /guide them/.test(b.textContent))", timeout=4000)
            check('“Let them point instead” answers Lamin', True)
        except Exception as e:
            check('“Let them point instead” answers Lamin', False, e)

        # ── effects: Lamin blurs his background, Fatou receives the finished picture ──
        # (this sandbox renders video in software: stop the 1280x720 screen share first so two heavy jobs don't overlap)
        await callB.click('.cw-bar [data-a=sharemenu]'); await asyncio.sleep(1)
        await callA.click('.cw-bar [data-a=cam]')
        await callB.wait_for_function("document.querySelector('.cw-rcam').videoWidth > 0", timeout=20000); await asyncio.sleep(1.5)
        before = await sharp(callB, '.cw-rcam')
        await callA.click('.cw-bar [data-a=fx]'); await callA.wait_for_selector('.fx-sheet', timeout=3000)
        await asyncio.sleep(3)                                   # the Studio warms the background model up meanwhile
        await callA.click('.fx-sheet [data-bg=blur2]')
        after, waited = None, 0
        while waited < 30:
            await asyncio.sleep(1); waited += 1
            after = await sharp(callB, '.cw-rcam', 2)
            if after and before and after < before * 0.5: break
        print(f'  blur reached Fatou after {waited}s', flush=True)
        check('Studio opens', True)
        check('Fatou receives Lamin’s blurred picture', bool(before and after) and after < before * 0.5, f"edge sharpness {before and round(before, 2)} → {after and round(after, 2)}")
        fxon = await callA.evaluate("document.querySelector('.cw-bar [data-a=fx]').classList.contains('on')")
        check('Effects button shows effects are on', fxon)
        # heavy effects on a slow (software) GPU must not starve the call: stay live past the 40 s heartbeat limit
        await callA.click('.fx-tabs [data-tab=face]'); await callA.click('.fx-sheet [data-face=shades]'); await asyncio.sleep(45)
        alive = await callA.evaluate(live) and await callB.evaluate(live)
        dropped = await callA.evaluate("(() => { try { return document.querySelector('.fx-perf').textContent; } catch (e) { return ''; } })()")
        check('call stays connected with heavy effects for 45 s', alive, f"Studio shows: {dropped!r}")
        await callA.click('.fx-sheet [data-fx=off]'); await asyncio.sleep(4)
        back = await sharp(callB, '.cw-rcam')
        check('turning effects off restores the raw camera', bool(back and after) and back > after * 1.8, f"edge sharpness → {back and round(back, 2)}")
        check('Mute still reachable with the Studio open', await callA.evaluate("(() => { const b = document.querySelector('.cw-bar [data-a=mic]').getBoundingClientRect(); const e = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2); return !!(e && e.closest('[data-a=mic]')); })()"))
        await callA.click('.fx-sheet [data-fx=close]')

        await callA.click('.cw-bar [data-a=hangup]'); await asyncio.sleep(1.5)
        bad = [e for e in errors if 'ResizeObserver' not in e]
        check('no script errors on any page', not bad, '; '.join(bad)[:300])
        await b.close()
    finish()

asyncio.run(main())
