/* Extra QR controls for TapTap Voucher Designer.
 * Uses the existing editor setter so save/undo/history remain native.
 */
(function () {
  var props = document.getElementById('props'), cfgNode = document.getElementById('cfgData');
  if (!props || !cfgNode) return;
  var initial; try { initial = JSON.parse(cfgNode.textContent || '{}'); } catch (e) { initial = {}; }
  var values = {};
  (initial.elements || []).forEach(function (e) { if (e.type === 'qr') values[e.id] = e.content || initial.qr_mode || 'login'; });

  function selectedId() {
    var n = document.querySelector('#canvas [data-eid].is-sel');
    return n ? n.getAttribute('data-eid') : '';
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; });
  }
  function decodeCustom(mode) {
    var raw = String(mode || '').slice(7);
    try { return decodeURIComponent(raw); } catch (e) { return raw; }
  }
  function transport(value) {
    var seg = props.querySelector('[data-seg="content"]'), b = seg && seg.querySelector('button[data-v]');
    if (!b) return false;
    var id = selectedId(), old = b.dataset.v;
    b.dataset.v = value; b.click(); b.dataset.v = old;
    if (id) values[id] = value;
    return true;
  }
  function installStyle() {
    if (document.getElementById('tt-qr-extra-style')) return;
    var s = document.createElement('style'); s.id = 'tt-qr-extra-style';
    s.textContent = '.tt-qr-extra{border:1px solid #d7e0ea;border-radius:12px;padding:.65rem;margin:.7rem 0;background:#f8fbff}.tt-qr-extra .modes{display:grid;grid-template-columns:1fr 1fr;gap:.4rem;margin-bottom:.55rem}.tt-qr-extra .modes button{border:1px solid #ccd9e6;background:#fff;border-radius:8px;padding:.5rem;text-align:left;font:600 .74rem system-ui;color:#20364d}.tt-qr-extra .modes button.on{border-color:#1769e0;background:#edf4ff;color:#1769e0}.tt-qr-extra textarea{width:100%;min-height:88px;border:1px solid #cad5df;border-radius:8px;padding:.55rem;font:12px ui-monospace,monospace;resize:vertical}.tt-qr-extra .tokens{display:flex;flex-wrap:wrap;gap:.25rem;margin:.4rem 0}.tt-qr-extra .tokens button{border:0;border-radius:999px;background:#e9eef5;padding:.2rem .42rem;font-size:.68rem}.tt-qr-note{font-size:.69rem;color:#6d7c8d;line-height:1.4}';
    document.head.appendChild(s);
  }
  function render() {
    var seg = props.querySelector('[data-seg="content"]');
    if (!seg || props.querySelector('.tt-qr-extra')) return;
    var id = selectedId(), mode = values[id] || '', stock = seg.querySelector('button.on[data-v]');
    if ((!mode || (!/^portal$/.test(mode) && mode.indexOf('custom:') !== 0)) && stock) mode = stock.dataset.v;
    values[id] = mode || 'login';
    var custom = values[id].indexOf('custom:') === 0 ? decodeCustom(values[id]) : '';
    var box = document.createElement('div'); box.className = 'tt-qr-extra';
    box.innerHTML =
      '<div style="font-weight:800;font-size:.76rem;margin-bottom:.4rem">More QR destinations</div>'+
      '<div class="modes"><button type="button" data-extra="portal"><b>Online customer portal</b><br><small>Always opens the current published TapTap portal</small></button><button type="button" data-extra="custom"><b>Custom QR content</b><br><small>URL, text, contact, WhatsApp, vCard…</small></button></div>'+
      '<div class="custom-box"'+(values[id].indexOf('custom:')===0?'':' hidden')+'><textarea maxlength="1200" placeholder="Anything a QR code can contain…">'+esc(custom)+'</textarea>'+
      '<div class="tokens"><button type="button" data-token="{code}">{code}</button><button type="button" data-token="{serial}">{serial}</button><button type="button" data-token="{business}">{business}</button><button type="button" data-token="{phone}">{phone}</button><button type="button" data-token="{ssid}">{ssid}</button></div>'+
      '<button type="button" class="btn btn-sm btn-primary apply-custom">Apply custom QR</button>'+
      '<div class="tt-qr-note mt-1">Examples: https://example.com/offers · tel:+2207000000 · mailto:help@example.com · plain instructions · WIFI:S:MyWiFi;T:nopass;; · a vCard. Voucher tokens are replaced on every printed card.</div></div>'+
      '<div class="tt-qr-note portal-note"'+(values[id]==='portal'?'':' hidden')+'>Printed QR uses a stable <b>/c/?v=CODE</b> TapTap link. You can redesign or replace the published customer portal later without reprinting the vouchers.</div>';
    seg.parentNode.insertBefore(box, seg.nextSibling);
    var p=box.querySelector('[data-extra="portal"]'), c=box.querySelector('[data-extra="custom"]');
    if(values[id]==='portal')p.classList.add('on'); if(values[id].indexOf('custom:')===0)c.classList.add('on');
    p.onclick=function(){values[id]='portal';transport('portal');};
    c.onclick=function(){box.querySelector('.custom-box').hidden=false;box.querySelector('.portal-note').hidden=true;box.querySelector('textarea').focus();};
    box.querySelector('.apply-custom').onclick=function(){
      var raw=box.querySelector('textarea').value||''; if(!raw.trim()){alert('Enter the web address, text or information for the QR code.');return;}
      var value='custom:'+encodeURIComponent(raw);values[id]=value;transport(value);
    };
    box.querySelectorAll('[data-token]').forEach(function(b){b.onclick=function(){
      var ta=box.querySelector('textarea'),a=ta.selectionStart==null?ta.value.length:ta.selectionStart,z=ta.selectionEnd==null?a:ta.selectionEnd;
      ta.value=ta.value.slice(0,a)+b.dataset.token+ta.value.slice(z);ta.focus();ta.selectionStart=ta.selectionEnd=a+b.dataset.token.length;
    };});
  }
  document.addEventListener('click',function(ev){
    var b=ev.target.closest&&ev.target.closest('#props [data-seg="content"] button[data-v]');if(!b)return;
    var id=selectedId();if(id)values[id]=b.dataset.v;
  },true);
  installStyle();
  var observer=new MutationObserver(function(){clearTimeout(observer._t);observer._t=setTimeout(render,20);});
  observer.observe(props,{childList:true,subtree:true});render();
})();
