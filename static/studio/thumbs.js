/* Live mini-previews for portal pages/templates: <iframe data-thumb="key"> + window.TP_THUMBS[key] = config */
(function(){
  var R = document.currentScript.getAttribute('data-renderer');
  window.tpThumbDoc = function(cfg, ctx){
    var c = JSON.stringify(cfg).replace(/</g,'\\u003c'), x = JSON.stringify(ctx).replace(/</g,'\\u003c');
    return '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=390"><script src="'+R+'"><\/script></head><body><div id="tp"></div><script>TapPortal.render(document.getElementById("tp"),'+c+','+x+');<\/script></body></html>';
  };
  function scale(f){ var w = f.parentElement.clientWidth; if (w) f.style.setProperty('--s', (w/390).toFixed(4)); }
  window.addEventListener('resize', function(){ document.querySelectorAll('iframe[data-thumb]').forEach(scale); });
  window.tpThumbs = function(root){
    (root||document).querySelectorAll('iframe[data-thumb]').forEach(function(f){
      scale(f); if (f.dataset.done) return; var cfg = (window.TP_THUMBS||{})[f.dataset.thumb]; if(!cfg) return;
      f.dataset.done = 1; f.srcdoc = tpThumbDoc(cfg, Object.assign({mode:'thumb'}, window.TP_CTX||{}));
    });
  };
})();
