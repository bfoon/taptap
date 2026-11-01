"""The sharer's TapTap tab, driven exactly as the call window drives it (BroadcastChannel 'tt-call'):
guardian, ghost cursor, action log, privacy limits, pause, Esc, receipts, marks that stay on scrolled content."""
import asyncio, json, os, sqlite3, sys
from playwright.async_api import async_playwright
from common import BASE, check, errors, finish, launch, setup
from make_media import camera_y4m

S = setup()

async def main():
    async with async_playwright() as p:
        b = await launch(p)
        ctx = await b.new_context(viewport={'width': 1200, 'height': 800})
        await ctx.add_cookies([{'name': 'sessionid', 'value': S['sharer'], 'url': BASE}])
        tab = await ctx.new_page(); tab.on('pageerror', lambda e: errors.append(str(e)))
        await tab.goto(BASE + '/'); await tab.wait_for_function("!!sessionStorage.getItem('tt-tab')")
        TAB = await tab.evaluate("sessionStorage.getItem('tt-tab')")
        # test controls on her page (normal page content: they scroll with it)
        await tab.evaluate("""() => {
          const box = document.createElement('div'); box.id = 'tt-test'; box.style.cssText = 'position:absolute;left:100px;top:120px;z-index:99999;background:#fff;padding:10px';
          box.innerHTML = '<button id="save">Save plan</button> <button id="del">Delete voucher</button> <label for="code">Voucher code</label><input id="code"> <input id="pw" type="password" name="password"><div style="height:2400px"></div>';
          document.body.appendChild(box); window.hits = { save: 0, del: 0 };
          document.getElementById('save').addEventListener('click', () => hits.save++); document.getElementById('del').addEventListener('click', () => hits.del++);
        }""")
        # the "call window": another page on the same channel, recording what the tab sends back
        cw = await ctx.new_page(); await cw.goto(BASE + '/static/js/chat.js')
        await cw.evaluate("""() => { window.bc = new BroadcastChannel('tt-call'); window.got = []; bc.onmessage = e => got.push(e.data);
          window.say = m => bc.postMessage(m); }""")
        vw, vh = await tab.evaluate('[innerWidth, innerHeight]')

        async def center(sel):
            r = await tab.evaluate(f"(() => {{ const r = document.querySelector('{sel}').getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }})()")
            return r[0] / vw, r[1] / vh

        async def say(m):
            await cw.evaluate('m => say(m)', m); await asyncio.sleep(0.25)

        async def got(t):
            return await cw.evaluate(f"got.filter(m => m.t === '{t}')")

        base = {'handle': TAB, 'who': 'Lamin', 'fw': 0, 'fh': 0}
        # ── control on ──
        await say(dict(base, t='rc-mode', on=True))
        bar = await tab.evaluate("(document.querySelector('.tt-rcbar') || {}).textContent || ''")
        check('her tab shows who is in control', 'Lamin' in bar and 'controlling' in bar, bar)
        pages = await got('rc-page')
        check('the log learns which page is open', bool(pages) and pages[0]['path'] == '/', pages)

        # ── ghost cursor names what the next click would hit ──
        x, y = await center('#save')
        await say(dict(base, t='laser', x=x, y=y, ctl=True, surface='browser'))
        lab = await tab.evaluate("document.querySelector('.tt-laser span').textContent")
        hov = await tab.evaluate("(() => { const h = document.querySelector('.tt-hov'); return h && !h.classList.contains('off') && !h.classList.contains('risky'); })()")
        check('ghost cursor: “Lamin → Save plan” and the button outlined', 'Save plan' in lab and hov, lab)
        x2, y2 = await center('#del')
        await say(dict(base, t='laser', x=x2, y=y2, ctl=True, surface='browser'))
        risky = await tab.evaluate("document.querySelector('.tt-hov').classList.contains('risky')")
        check('a risky button is outlined in red before it is clicked', risky)

        # ── a normal click happens and is logged ──
        await say(dict(base, t='rc', op='click', x=x, y=y))
        hits = await tab.evaluate('hits')
        acts = await got('rc-act')
        check('remote click on “Save plan” works', hits['save'] == 1, hits)
        check('…and is reported for the log by name', any(a['label'] == 'Save plan' and a['ok'] for a in acts), acts[-1:] if acts else acts)

        # ── guardian: a risky click waits for the owner ──
        await say(dict(base, t='rc', op='click', x=x2, y=y2))
        g = await tab.evaluate("(document.querySelector('.tt-guard') || {}).textContent || ''")
        check('“Delete voucher” is held for the owner’s OK', 'Delete voucher' in g and (await tab.evaluate('hits.del')) == 0, g[:80])
        res = await got('rc-res')
        check('Lamin is told it is waiting', any('Waiting' in r.get('why', '') for r in res), res[-1:] if res else res)
        await tab.evaluate("document.querySelector('.tt-guard [data-g=ok]').dispatchEvent(new MouseEvent('click', {bubbles: true}))")   # a scripted (remote-style) click
        await asyncio.sleep(0.2)
        check('a scripted click cannot approve it', (await tab.evaluate('hits.del')) == 0 and await tab.evaluate("!!document.querySelector('.tt-guard')"))
        await say(dict(base, t='rc', op='click', x=(await center('.tt-guard [data-g=ok]'))[0], y=(await center('.tt-guard [data-g=ok]'))[1]))
        check('nor can the controller click “Allow once” remotely', (await tab.evaluate('hits.del')) == 0 and await tab.evaluate("!!document.querySelector('.tt-guard')"))
        await tab.click('.tt-guard [data-g=ok]')                          # Fatou's own (trusted) tap
        await asyncio.sleep(0.2)
        check('the owner’s own tap lets it through once', (await tab.evaluate('hits.del')) == 1 and not await tab.evaluate("!!document.querySelector('.tt-guard')"))
        await say(dict(base, t='rc', op='click', x=x2, y=y2)); await tab.click('.tt-guard [data-g=no]'); await asyncio.sleep(0.2)
        res = await got('rc-res')
        check('“Block” stops it and tells Lamin', (await tab.evaluate('hits.del')) == 1 and 'Blocked' in res[-1].get('why', ''), res[-1])

        # ── passwords stay private; typing works and only the field name is logged ──
        xp, yp = await center('#pw')
        await say(dict(base, t='rc', op='click', x=xp, y=yp))
        res = await got('rc-res')
        check('password field cannot be clicked remotely', 'Passwords' in res[-1].get('why', ''), res[-1])
        xc, yc = await center('#code')
        await say(dict(base, t='rc', op='click', x=xc, y=yc)); await say(dict(base, t='rc', op='text', text='ABC123'))
        val = await tab.evaluate("document.getElementById('code').value"); acts = await got('rc-act')
        typed = [a for a in acts if a['op'] == 'type']
        check('typing into “Voucher code” works', val == 'ABC123', val)
        check('the log says where they typed, never what', typed and 'Voucher code' in typed[-1]['label'] and 'ABC123' not in json.dumps(acts), typed[-1:] if typed else acts[-2:])

        # ── pause ──
        await say(dict(base, t='rc-mode', on=True, hold=True))
        await say(dict(base, t='rc', op='click', x=x, y=y))
        bar = await tab.evaluate("document.querySelector('.tt-rcbar').textContent")
        check('paused control: nothing is clicked, the bar says paused', (await tab.evaluate('hits.save')) == 1 and 'paused' in bar, bar)
        await say(dict(base, t='rc-mode', on=True, hold=False))

        # ── Esc (a real key press by the owner) takes control back ──
        await tab.bring_to_front(); await tab.keyboard.press('Escape'); await asyncio.sleep(0.3)
        stop = await got('rc-stop')
        check('Esc on her keyboard takes control back', bool(stop) and not await tab.evaluate("!!document.querySelector('.tt-rcbar')"), stop)
        await say(dict(base, t='rc-mode', on=False))

        # ── ripples and drawings stay on the content when the page scrolls ──
        await tab.evaluate("window.__p = null; new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(n => { if (n.classList && n.classList.contains('tt-ptr')) window.__p = n; }))).observe(document.body, {childList: true})")
        await say(dict(base, t='ptr', x=x, y=y, id='abc', surface='browser'))
        seen = await got('ptr-seen')
        check('the tab sends a receipt that it showed the ripple', bool(seen) and seen[-1]['id'] == 'abc' and seen[-1]['visible'], seen[-1:] if seen else seen)
        top0 = await tab.evaluate('parseFloat(window.__p.style.top)')
        await tab.evaluate('window.scrollBy(0, 100)'); await asyncio.sleep(0.25)
        top1 = await tab.evaluate('parseFloat(window.__p.style.top)')
        check('the ripple moves with the button when the page scrolls', abs((top0 - top1) - 100) < 6, f'{top0:.0f} → {top1:.0f}')
        await tab.evaluate('window.scrollTo(0, 0)')
        await say(dict(base, t='ink', id='s1', pts=[[0.5, 0.5], [0.6, 0.5]], c='#ff0000', w=6, end=True, surface='browser'))
        red_at = "(y) => { const c = document.querySelector('.tt-ink'), g = c.getContext('2d'), d = devicePixelRatio || 1; return g.getImageData(Math.round(innerWidth * 0.55 * d), Math.round(y * d), 1, 1).data[0]; }"
        r0 = await tab.evaluate(red_at, vh * 0.5)
        await tab.evaluate('window.scrollBy(0, 120)'); await asyncio.sleep(0.25)
        r1, r2 = await tab.evaluate(red_at, vh * 0.5), await tab.evaluate(red_at, vh * 0.5 - 120)
        check('a drawing stays on the content when the page scrolls', r0 > 200 and r1 < 50 and r2 > 200, f'red at line {r0} → after scroll: old spot {r1}, new spot {r2}')
        await say(dict(base, t='ptr', x=0.5, y=0.5, id='zzz', surface='browser', handle='another-tab'))
        check('ripples meant for another tab are ignored', not any(s_['id'] == 'zzz' for s_ in await got('ptr-seen')))
        check('no script errors on her page', not errors, '; '.join(errors)[:200])
        await b.close()
    finish()

asyncio.run(main())
