"""The camera effects engine (static/js/call-fx.js) in a real browser: a synthetic camera made from a public-domain
portrait on a checkerboard. Checks background blur and replacement, masks drawn on the face, a drawing pinned to
the face following the head, the name tag, and that nothing leaks while the background model loads."""
import asyncio, base64, json, sys
from playwright.async_api import async_playwright
from common import BASE, check, finish, launch
from make_media import portrait_png


def portrait_url():
    return 'data:image/png;base64,' + base64.b64encode(portrait_png().read_bytes()).decode()


HARNESS = '<!doctype html><html><head><meta charset="utf-8"></head><body>\n<video id="out" muted playsinline autoplay style="width:640px;height:360px"></video>\n<script src="/static/js/call-fx.js"></script>\n<script>\nconst R = window.R = { log: [] };\nconst img = new Image(); img.src = window.PORTRAIT;\nlet offset = 140;\nwindow.setOffset = v => { offset = v; };\nconst cam = document.createElement(\'canvas\'); cam.width = 640; cam.height = 360; const cg = cam.getContext(\'2d\');\nfunction drawCam() {\n  for (let y = 0; y < 360; y += 20) for (let x = 0; x < 640; x += 20) { cg.fillStyle = ((x + y) / 20) % 2 ? \'#ffffff\' : \'#000000\'; cg.fillRect(x, y, 20, 20); }\n  if (img.complete) cg.drawImage(img, offset, 0, 360, 360);\n  requestAnimationFrame(drawCam);\n}\nimg.onload = () => {\n  drawCam();\n  const track = cam.captureStream(24).getVideoTracks()[0];\n  const fx = window.fx = TTCallFX.engine({ base: \'/static/vendor/mediapipe/\', name: \'Ada Test\', title: \'TapTap Support\' });\n  const out = fx.attach(track);\n  document.getElementById(\'out\').srcObject = new MediaStream([out]);\n  R.ready = true; R.path = window.MediaStreamTrackGenerator ? \'generator\' : \'canvas\';\n};\nconst probe = document.createElement(\'canvas\'); probe.width = 640; probe.height = 360; const pg = probe.getContext(\'2d\', { willReadFrequently: true });\nwindow.grab = (src) => { pg.drawImage(src === \'in\' ? cam : document.getElementById(\'out\'), 0, 0, 640, 360); return pg; };\nwindow.region = (src, x, y, w, h) => {      // mean + variance of luma in a box\n  const d = grab(src).getImageData(x, y, w, h).data; let s = 0, s2 = 0, n = 0, r = 0, g = 0, b = 0;\n  for (let i = 0; i < d.length; i += 4) { const l = d[i] * .299 + d[i + 1] * .587 + d[i + 2] * .114; s += l; s2 += l * l; n++; r += d[i]; g += d[i + 1]; b += d[i + 2]; }\n  return { mean: +(s / n).toFixed(1), var: +(s2 / n - (s / n) ** 2).toFixed(1), rgb: [Math.round(r / n), Math.round(g / n), Math.round(b / n)] };\n};\nwindow.redCentroid = () => {     // where the bright red ink is\n  const d = grab(\'out\').getImageData(0, 0, 640, 360).data; let sx = 0, sy = 0, n = 0;\n  for (let i = 0; i < d.length; i += 4) if (d[i] > 200 && d[i + 1] < 60 && d[i + 2] < 60) { const p = i / 4; sx += p % 640; sy += Math.floor(p / 640); n++; }\n  return n ? { x: Math.round(sx / n), y: Math.round(sy / n), n } : null;\n};\n</script></body></html>\n'


async def main():
    async with async_playwright() as p:
        b = await launch(p, '--autoplay-policy=no-user-gesture-required')
        pg = await b.new_page(); errs=[]
        pg.on('pageerror', lambda e: errs.append(str(e)))
        await pg.goto(BASE + '/static/vendor/mediapipe/NOTICE.md')          # same origin as the app, then the harness
        await pg.set_content(HARNESS.replace('window.PORTRAIT', json.dumps(portrait_url())))
        await pg.wait_for_function('window.R && R.ready', timeout=20000)
        await pg.wait_for_timeout(800)
        path = await pg.evaluate('R.path')
        # 1 pass-through
        a = await pg.evaluate("region('in', 0, 0, 120, 120)"); o = await pg.evaluate("region('out', 0, 0, 120, 120)")
        check('inactive = untouched picture', abs(a['var']-o['var']) < a['var']*0.15, f"path={path} in.var={a['var']} out.var={o['var']}")
        # 2 blur background (privacy blur first, then the real cut-out)
        await pg.evaluate("fx.set({bg:'blur2'})")
        await pg.wait_for_timeout(300)
        early = await pg.evaluate("region('out', 0, 0, 120, 120)")
        check('room hidden while model loads', early['var'] < a['var']*0.5, f"var {a['var']} -> {early['var']}")
        await pg.wait_for_function('fx.mask.ready', timeout=60000); await pg.wait_for_timeout(1200)
        bgv = await pg.evaluate("region('out', 0, 0, 120, 120)"); facein = await pg.evaluate("region('in', 300, 90, 60, 60)"); faceout = await pg.evaluate("region('out', 300, 90, 60, 60)")
        check('background blurred', bgv['var'] < a['var']*0.3, f"corner var {a['var']} -> {bgv['var']}")
        check('person stays sharp', abs(facein['var']-faceout['var']) < max(80, facein['var']*0.35) and abs(facein['mean']-faceout['mean']) < 25, f"face var {facein['var']} -> {faceout['var']}, mean {facein['mean']} -> {faceout['mean']}")
        check('mask alpha detected', (await pg.evaluate('fx.maskAlpha')) is not None, str(await pg.evaluate('fx.maskAlpha')))
        # 3 scene
        await pg.evaluate("fx.set({bg:'scene:night'})"); await pg.wait_for_timeout(700)
        night = await pg.evaluate("region('out', 0, 300, 80, 60)")
        check('night scene replaces the room', night['rgb'][2] > night['rgb'][0] and night['mean'] < 80, str(night))
        await pg.evaluate("fx.set({bg:'color:#18a66a'})"); await pg.wait_for_timeout(600)
        green = await pg.evaluate("region('out', 0, 0, 60, 60)")
        check('plain colour background', green['rgb'][1] > 140 and green['rgb'][0] < 60, str(green))
        await pg.evaluate("fx.set({bg:'none'})")
        # 4 face mask
        await pg.evaluate("fx.set({face:'shades'})")
        await pg.wait_for_function('fx.face.lm', timeout=60000); await pg.wait_for_timeout(800)
        lm = await pg.evaluate("(()=>{const L=fx.face.lm;return {a:[L[33][0]*640,L[33][1]*360],b:[L[263][0]*640,L[263][1]*360]}})()")
        ex, ey = int((lm['a'][0]+lm['b'][0])/2), int((lm['a'][1]+lm['b'][1])/2)
        ea = int(lm['a'][0]+ (lm['b'][0]-lm['a'][0])*0.2)
        ein = await pg.evaluate(f"region('in', {ea-6}, {ey-4}, 12, 8)"); eout = await pg.evaluate(f"region('out', {ea-6}, {ey-4}, 12, 8)")
        check('face found where the face is', 270 < ex < 330 and 50 < ey < 95, f"eyes at ({ex},{ey}); photo face is at ~(298,72)")
        check('shades drawn over the eyes', eout['mean'] < ein['mean'] - 25 and eout['mean'] < 70, f"eye luma {ein['mean']} -> {eout['mean']}")
        for f in ['glasses','masq','hero','cat','crown','headset','stache','party','halo','emoji','pixel']:
            await pg.evaluate(f"fx.set({{face:'{f}'}})"); await pg.wait_for_timeout(250)
        check('every mask draws without errors', not errs, '; '.join(errs)[:200])
        await pg.evaluate("fx.set({face:'none'})")
        # 5 stick-to-face ink follows the head
        await pg.evaluate("fx.ink.tool='pen'; fx.ink.color='#ff0000'; fx.ink.stick=true; fx.ink.width=6")
        nose = await pg.evaluate("[fx.face.lm[1][0], fx.face.lm[1][1]]")
        await pg.evaluate(f"(()=>{{const x={nose[0]},y={nose[1]}; fx.inkStart(x-0.04,y+0.03,640,360); for(let i=0;i<=10;i++) fx.inkAdd(x-0.04+i*0.008,y+0.03,640,360); fx.inkEnd();}})()")
        await pg.wait_for_timeout(500)
        c1 = await pg.evaluate('redCentroid()')
        await pg.evaluate('setOffset(80)'); await pg.wait_for_timeout(3500)
        c2 = await pg.evaluate('redCentroid()')
        moved = (c1 and c2) and (c1['x'] - c2['x'])
        check('drawing pinned to the face moves with it', bool(c1 and c2) and 50 < moved < 70 and abs(c1['y']-c2['y']) < 15, f"{c1} -> {c2} (head moved 60px)")
        await pg.evaluate('setOffset(140); fx.clearInk()')
        # frame ink stays put
        await pg.evaluate("fx.ink.stick=false; fx.inkStart(0.1,0.5,640,360); fx.inkAdd(0.15,0.5,640,360); fx.inkEnd()"); await pg.wait_for_timeout(400)
        c3 = await pg.evaluate('redCentroid()')
        check('normal drawing stays on the picture', bool(c3) and abs(c3['x']-80) < 20 and abs(c3['y']-180) < 10, str(c3))
        await pg.evaluate('fx.clearInk()')
        # 6 name tag
        await pg.evaluate("fx.set({tag:true})"); await pg.wait_for_timeout(500)
        tag = await pg.evaluate("region('out', 20, 300, 60, 40)")
        check('name tag shown bottom-left', tag['mean'] < 120 and tag['var'] > 300, str(tag))
        ms = await pg.evaluate('fx.stats.ms'); fps = await pg.evaluate('fx.stats.fps')
        check('drawing a frame is cheap (models aside)', ms < 25, f"{ms:.1f} ms per frame; {fps} fps here with a software-emulated GPU")
        await pg.evaluate('fx.off()'); await pg.wait_for_timeout(400)
        check('turn everything off', not await pg.evaluate('fx.active()'))
        await b.close()
    finish()
asyncio.run(main())
