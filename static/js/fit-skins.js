/* Fit's wardrobe — what each look adds to the robot (names, greetings and the calendar: core/fit_skins.py).
   The robot is drawn in a 100 × 120 box (static/js/fit.js): head 22–78 × 12–56 (visor 28–72 × 37–53, eyes at
   39.5 / 60.5, 44.5), torso 30–70 × 58–92, arms at 23–32 and 68–77, the flame below 96.
   Layers (all optional):
     pal       colours: s s2 s3 (metal) · a a2 (dome + band) · v (visor) · l (eyes and mouth LEDs)
     back      behind the whole body (capes, tails, brooms, backpacks)
     headBack  behind the head (ears, manes, antlers, collars)
     visor     on the visor, under the eyes (panda patches)
     head      on top of the head (hats, masks, beards) — eyes are always left visible
     body      on the torso (scarves, belts, robes, emblems)
     top       floating around (lanterns, moons, parrots, flags)
     fx        little particles: snow · bats · stars · gold · confetti · gm · hearts
     noAntenna hides the antenna (hats sit on top)
     css       extra rules, scoped to .sk-<id> */
(function () {
  'use strict';
  function mane() {
    var s = '';
    for (var i = 0; i < 16; i++) {
      var a = i / 16 * Math.PI * 2, x = 50 + Math.cos(a) * 29, y = 35 + Math.sin(a) * 27, deg = a * 180 / Math.PI + 90;
      s += '<ellipse cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" rx="7" ry="11" transform="rotate(' + deg.toFixed(0) + ' ' + x.toFixed(1) + ' ' + y.toFixed(1) +
        ')" fill="' + (i % 2 ? '#92400e' : '#b45309') + '"/>';
    }
    return s;
  }
  var STAR = function (x, y, k, fill) {     // a small 5-point star at x,y scaled by k
    return '<path transform="translate(' + x + ' ' + y + ') scale(' + k + ')" d="M0 -5 L1.5 -1.6 5 -1.5 2.3 .9 3.1 4.5 0 2.6 -3.1 4.5 -2.3 .9 -5 -1.5 -1.5 -1.6 Z" fill="' + fill + '"/>';
  };
  var WHISKERS = function (c) {
    return '<g stroke="' + c + '" stroke-width=".9" stroke-linecap="round" opacity=".9"><path d="M24 46 L10 43 M24 49 L10 50 M76 46 L90 43 M76 49 L90 50"/></g>';
  };

  var L = {
    classic: {},

    robot_gold: {
      pal: { s: '#fff3bf', s2: '#ffd43b', s3: '#a87b00', a: '#f08c00', a2: '#b35c00', v: '#1c1300', l: '#fff9db' },
      head: '<path d="M37 13 L40 3 L45 9 L50 0 L55 9 L60 3 L63 13 Z" fill="#ffd43b" stroke="#a87b00" stroke-width=".7"/>' +
        '<circle cx="50" cy="4" r="1.4" fill="#e03131"/><circle cx="41" cy="6" r="1" fill="#1c7ed6"/><circle cx="59" cy="6" r="1" fill="#2f9e44"/>',
      noAntenna: true, fx: 'gold',
      css: '.sk-robot_gold .fit-bot{filter:drop-shadow(0 0 5px rgba(255,212,59,.6))}'
    },

    // ───── Christmas & New Year ─────
    santa: {
      pal: { a: '#d62828', a2: '#9d1b1b' },
      head: '<path d="M30 21 Q45 2 70 5 Q87 8 87 21 L80 23 Q76 13 62 14 Z" fill="#d62828" stroke="#9d1b1b" stroke-width=".6"/>' +
        '<circle class="sk-pom" cx="85" cy="22" r="4.6" fill="#fff"/>' +
        '<rect x="17" y="28.5" width="66" height="8" rx="4" fill="#fff" stroke="#e5e7eb" stroke-width=".6"/>' +
        '<g fill="#fff" stroke="#e5e7eb" stroke-width=".5"><circle cx="34" cy="56" r="6"/><circle cx="42" cy="60" r="6.5"/><circle cx="50" cy="62" r="7"/><circle cx="58" cy="60" r="6.5"/><circle cx="66" cy="56" r="6"/></g>' +
        '<path d="M41 54.5 Q45.5 51.6 50 54 Q54.5 51.6 59 54.5 Q54.5 57.5 50 55.6 Q45.5 57.5 41 54.5 Z" fill="#fff" stroke="#e5e7eb" stroke-width=".4"/>',
      body: '<rect x="30" y="83" width="40" height="6" fill="#1f2937"/><rect x="45" y="82" width="10" height="8" rx="1.5" fill="none" stroke="#fbbf24" stroke-width="1.8"/>',
      noAntenna: true, fx: 'snow'
    },
    snowman: {
      pal: { s: '#ffffff', s2: '#dbe7f3', s3: '#94a9c0', a: '#eef4fb', a2: '#cfdbe8', v: '#16263a' },
      head: '<rect x="32" y="1" width="36" height="25" rx="2" fill="#111827"/><rect x="32" y="18" width="36" height="4" fill="#dc2626"/>' +
        '<rect x="22" y="24" width="56" height="5" rx="2.5" fill="#111827"/>' +
        '<path d="M49 46.5 L60 48 L49 49.5 Z" fill="#f97316"/>',
      body: '<rect x="31" y="55" width="38" height="7" rx="3.5" fill="#dc2626"/><path d="M57 58 L65 58 L63 78 L56 77 Z" fill="#dc2626"/>' +
        '<path d="M56.6 72 L63.8 72.6 M56.3 75 L63.4 75.6" stroke="#fff" stroke-width="1.2"/>',
      noAntenna: true, fx: 'snow'
    },
    reindeer: {
      pal: { s: '#d9a877', s2: '#a8703f', s3: '#6b4423', a: '#7a4a24', a2: '#5a3418', v: '#2a1a0e', l: '#ffe8b0' },
      headBack: '<g fill="none" stroke="#6b4423" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">' +
          '<path d="M33 20 L26 6 L18 2 M26 6 L28 -2 M29.5 13 L20 12"/><path d="M67 20 L74 6 L82 2 M74 6 L72 -2 M70.5 13 L80 12"/></g>' +
        '<ellipse cx="18" cy="27" rx="7" ry="3.5" transform="rotate(-25 18 27)" fill="#a8703f"/><ellipse cx="82" cy="27" rx="7" ry="3.5" transform="rotate(25 82 27)" fill="#a8703f"/>',
      head: '<circle class="sk-nose" cx="50" cy="47.5" r="3.4" fill="#ef4444"/><circle cx="49" cy="46.4" r="1" fill="#fff" opacity=".8"/>',
      body: '<rect x="33" y="55" width="34" height="5" rx="2.5" fill="#b91c1c"/><circle cx="50" cy="61" r="3.2" fill="#fbbf24" stroke="#b45309" stroke-width=".6"/>',
      noAntenna: true, fx: 'snow'
    },
    party: {
      pal: { a: '#7c3aed', a2: '#5b21b6' },
      head: '<path d="M38 15 L50 -14 L62 15 Z" fill="#facc15" stroke="#ca8a04" stroke-width=".6"/>' +
        '<path d="M42.1 5 L57.9 5 L59.1 8 L40.9 8 Z" fill="#ec4899"/><path d="M45.9 -4 L54.1 -4 L55.4 -1 L44.6 -1 Z" fill="#22c55e"/>' +
        '<circle class="sk-pom" cx="50" cy="-15" r="3.6" fill="#ec4899"/>',
      body: '<path d="M50 57 L42 53 L42 61 Z M50 57 L58 53 L58 61 Z" fill="#facc15"/><circle cx="50" cy="57" r="2" fill="#ca8a04"/>',
      noAntenna: true, fx: 'confetti'
    },

    // ───── Halloween masks ─────
    pumpkin: {
      pal: { s: '#ffa94d', s2: '#f76707', s3: '#a84300', a: '#e8590c', a2: '#a84300', v: '#1f0f02', l: '#ffcc33' },
      head: '<g fill="none" stroke="#c2410c" stroke-width="1.2" opacity=".75"><path d="M26 21 Q21 37 26 54"/><path d="M74 21 Q79 37 74 54"/><path d="M50 13 L50 30"/></g>' +
        '<path d="M47 14 Q46 6 50 2 L54 4 Q51 8 53 14 Z" fill="#2f9e44"/><path d="M53 8 Q62 2 66 8 Q60 12 53 10 Z" fill="#40c057"/>' +
        '<path d="M46 9 q-6 -2 -5 -7 q4 0 3 4" fill="none" stroke="#2f9e44" stroke-width="1.2"/>',
      noAntenna: true, fx: 'bats',
      css: '.sk-pumpkin .fb-eye{filter:drop-shadow(0 0 3px #ffb703)}'
    },
    vampire: {
      pal: { s: '#eef0f6', s2: '#c9cde0', s3: '#6d6a8a', a: '#1b1b2f', a2: '#0f0f1d', v: '#1a0b14', l: '#ff3355' },
      back: '<g class="sk-cape"><path d="M30 60 Q14 84 20 104 Q35 98 50 104 Q65 98 80 104 Q86 84 70 60 Z" fill="#111827"/>' +
        '<path d="M33 62 Q21 84 25 100 Q37 95 50 99 Q63 95 75 100 Q79 84 67 62 Z" fill="#7f1d1d"/></g>',
      headBack: '<path d="M27 56 L12 28 L33 45 Z" fill="#111827"/><path d="M73 56 L88 28 L67 45 Z" fill="#111827"/>' +
        '<path d="M27 54 L16 33 L31 46 Z M73 54 L84 33 L69 46 Z" fill="#991b1b"/>',
      head: '<path d="M23 33 C23 19 33 12.5 50 12.5 C67 12.5 77 19 77 33 L66 30.5 L50 36.5 L34 30.5 Z" fill="#1b1b2f"/>' +
        '<path d="M44.5 53 L46.3 57.5 L48 53 Z M52 53 L53.7 57.5 L55.5 53 Z" fill="#fff"/>',
      noAntenna: true, fx: 'bats'
    },
    mummy: {
      pal: { s: '#efe3c8', s2: '#d6c39d', s3: '#8f7a54', a: '#d9c7a1', a2: '#a8916a', v: '#14110c', l: '#9cff57' },
      head: '<g fill="#f4ead3" stroke="#bda57a" stroke-width=".6"><rect x="22" y="19" width="56" height="5" rx="2.5" transform="rotate(-6 50 21)"/>' +
          '<rect x="21" y="26" width="58" height="5" rx="2.5" transform="rotate(4 50 28)"/><rect x="25" y="13" width="40" height="4.5" rx="2.2" transform="rotate(8 45 15)"/>' +
          '<rect x="24" y="54" width="52" height="4.5" rx="2.2" transform="rotate(-3 50 56)"/></g>' +
        '<path class="sk-loose" d="M77 27 q8 4 6 14 q-1 6 4 10" fill="none" stroke="#f4ead3" stroke-width="3.5" stroke-linecap="round"/>',
      body: '<g fill="#f4ead3" stroke="#bda57a" stroke-width=".6"><rect x="29" y="59.5" width="42" height="4" rx="2" transform="rotate(5 50 61)"/>' +
        '<rect x="29" y="85" width="42" height="4.5" rx="2" transform="rotate(-4 50 87)"/></g>',
      noAntenna: true, fx: 'bats'
    },
    witch: {
      pal: { s: '#ece6ff', s2: '#c4b5fd', s3: '#6d5bd0', a: '#5b21b6', a2: '#3b0f86', l: '#b9ff66' },
      back: '<g class="sk-broom"><line x1="8" y1="110" x2="92" y2="80" stroke="#8b5a2b" stroke-width="3" stroke-linecap="round"/>' +
        '<path d="M12 107 L-2 116 M13 109 L1 120 M15 110 L5 123 M11 105 L-4 112" stroke="#d4a017" stroke-width="2" stroke-linecap="round"/></g>',
      head: '<ellipse cx="50" cy="17" rx="35" ry="5.5" fill="#1e1035"/>' +
        '<path d="M31 16 Q38 -6 52 -14 Q60 -18 66 -10 Q58 -10 60 -2 Q63 8 69 16 Z" fill="#1e1035"/>' +
        '<rect x="34" y="10" width="31" height="4.5" fill="#7c3aed"/><rect x="46.5" y="9.4" width="7" height="5.7" rx="1" fill="none" stroke="#facc15" stroke-width="1.4"/>',
      noAntenna: true, fx: 'stars'
    },
    skull: {
      pal: { s: '#3a3f4b', s2: '#262a33', s3: '#0f1115', a: '#e9ecef', a2: '#adb5bd', v: '#0b0b0b', l: '#ff6b6b' },
      head: '<path fill-rule="evenodd" fill="#f8f9fa" stroke="#ced4da" stroke-width=".6" d="M24 30 C24 16 34 12 50 12 C66 12 76 16 76 30 L76 46 C76 56 66 60 58 60 L42 60 C34 60 24 56 24 46 Z ' +
          'M33 44.5 a6.5 6.5 0 1 0 13 0 a6.5 6.5 0 1 0 -13 0 Z M54 44.5 a6.5 6.5 0 1 0 13 0 a6.5 6.5 0 1 0 -13 0 Z"/>' +
        '<path d="M48 51.5 L50 48 L52 51.5 Z" fill="#212529"/>' +
        '<rect x="40" y="53" width="20" height="5" rx="1.5" fill="#fff" stroke="#212529" stroke-width=".8"/><path d="M44 53v5M48 53v5M52 53v5M56 53v5" stroke="#212529" stroke-width=".7"/>' +
        '<g><circle cx="30" cy="21" r="3.4" fill="#f59f00"/><circle cx="38" cy="17" r="3" fill="#e64980"/><circle cx="50" cy="15" r="3.6" fill="#f59f00"/>' +
          '<circle cx="62" cy="17" r="3" fill="#e64980"/><circle cx="70" cy="21" r="3.4" fill="#f59f00"/>' +
          '<g fill="#fff3bf"><circle cx="30" cy="21" r="1"/><circle cx="38" cy="17" r=".9"/><circle cx="50" cy="15" r="1.1"/><circle cx="62" cy="17" r=".9"/><circle cx="70" cy="21" r="1"/></g></g>',
      noAntenna: true, fx: 'bats'
    },

    // ───── Eid, Tobaski & The Gambia ─────
    eid: {
      pal: { s: '#f4fbf7', s2: '#cfe9da', s3: '#3d7a5c', a: '#0f7a4a', a2: '#d4a017', v: '#082a1c', l: '#ffe08a' },
      head: '<path d="M27 27 L31 23 L35 27 L39 23 L43 27 L47 23 L51 27 L55 23 L59 27 L63 23 L67 27 L71 23 L74 26" fill="none" stroke="#f5c542" stroke-width="1.2" stroke-linejoin="round"/>' +
        '<circle cx="50" cy="17" r="1.6" fill="#f5c542"/><circle cx="40" cy="19" r="1.1" fill="#f5c542"/><circle cx="60" cy="19" r="1.1" fill="#f5c542"/>',
      top: '<g class="sk-float-l"><path d="M12 6 A9 9 0 1 0 12 24 A12 12 0 0 1 12 6 Z" fill="#f5c542"/>' + STAR(19, 10, .55, '#f5c542') + '</g>' +
        '<g class="sk-lantern"><line x1="90" y1="16" x2="90" y2="26" stroke="#d4a017" stroke-width="1"/><path d="M85 27 L95 27 L93 31 L87 31 Z" fill="#d4a017"/>' +
          '<circle class="sk-glow" cx="90" cy="36" r="8" fill="#ffd54a" opacity=".3"/>' +
          '<rect x="86" y="31" width="8" height="10" rx="1.5" fill="#ffe08a" stroke="#d4a017" stroke-width="1"/><path d="M90 31 v10 M86 36 h8" stroke="#d4a017" stroke-width=".7"/>' +
          '<path d="M86 41 L94 41 L92 44 L88 44 Z" fill="#d4a017"/></g>',
      noAntenna: true, fx: 'gold'
    },
    tobaski: {
      pal: { s: '#fdf7ec', s2: '#ecd9b8', s3: '#8a6a3a', a: '#1d4ed8', a2: '#d4a017', v: '#0b1739', l: '#7dd3fc' },
      head: '<path d="M29 24 L31 5 Q50 0 69 5 L71 24 Q50 20 29 24 Z" fill="#1d4ed8" stroke="#1e3a8a" stroke-width=".6"/>' +
        '<path d="M31 10 L35 7 L39 10 L43 7 L47 10 L51 7 L55 10 L59 7 L63 10 L67 7 L69 9" fill="none" stroke="#f5c542" stroke-width="1.2"/>' +
        '<path d="M30 19 Q50 15 70 19" fill="none" stroke="#f5c542" stroke-width="1.1"/>',
      body: '<path d="M28 58 L72 58 L77 94 L23 94 Z" fill="#1d4ed8"/><path d="M42 58 L50 70 L58 58" fill="none" stroke="#f5c542" stroke-width="1.6"/>' +
        '<path d="M38 60 Q50 80 62 60" fill="none" stroke="#f5c542" stroke-width="1" stroke-dasharray="2 1.5"/>' +
        '<g fill="#f5c542"><circle cx="50" cy="75" r="1.4"/><circle cx="45" cy="80" r="1"/><circle cx="55" cy="80" r="1"/><circle cx="50" cy="85" r="1"/></g>',
      top: '<g transform="translate(-6 92)"><g class="sk-ram"><ellipse cx="12" cy="14" rx="10" ry="7" fill="#fff" stroke="#d6d3d1"/>' +
        '<circle cx="21" cy="9" r="5" fill="#f5f5f4" stroke="#d6d3d1"/><path d="M19 6 q-5 -2 -4 3 q1 3 4 1" fill="none" stroke="#a16207" stroke-width="1.6"/>' +
        '<circle cx="22.5" cy="8.5" r=".9" fill="#111"/><path d="M6 20 v4 M16 20 v4" stroke="#57534e" stroke-width="1.6"/></g></g>',
      noAntenna: true, fx: 'gold'
    },
    gambia: {
      pal: { a: '#ce1126', a2: '#0c1c8c' },
      body: '<g transform="rotate(-35 50 75)"><rect x="26" y="66" width="48" height="4.6" fill="#ce1126"/><rect x="26" y="70.6" width="48" height="1" fill="#fff"/>' +
        '<rect x="26" y="71.6" width="48" height="3.4" fill="#0c1c8c"/><rect x="26" y="75" width="48" height="1" fill="#fff"/><rect x="26" y="76" width="48" height="4.6" fill="#3a7728"/></g>',
      top: '<g class="sk-flag"><line x1="88" y1="8" x2="88" y2="46" stroke="#6b7280" stroke-width="1.5" stroke-linecap="round"/>' +
        '<g class="sk-flag-cloth"><rect x="88" y="8" width="16" height="3.6" fill="#ce1126"/><rect x="88" y="11.6" width="16" height=".8" fill="#fff"/>' +
        '<rect x="88" y="12.4" width="16" height="2.6" fill="#0c1c8c"/><rect x="88" y="15" width="16" height=".8" fill="#fff"/><rect x="88" y="15.8" width="16" height="3.6" fill="#3a7728"/></g></g>',
      fx: 'gm'
    },

    // ───── Heroes & adventures ─────
    ninja: {
      pal: { s: '#3b3f46', s2: '#24272c', s3: '#0d0f12', a: '#1b1d21', a2: '#000000', v: '#05070a', l: '#f1f3f5' },
      back: '<line x1="16" y1="102" x2="82" y2="52" stroke="#111" stroke-width="3" stroke-linecap="round"/><line x1="72" y1="59.6" x2="86" y2="49" stroke="#b91c1c" stroke-width="3.8" stroke-linecap="round"/>',
      head: '<path fill-rule="evenodd" fill="#1b1d21" d="M21 32 C21 16 33 11 50 11 C67 11 79 16 79 32 L79 48 C79 56 72 58 64 58 L36 58 C28 58 21 56 21 48 Z ' +
          'M30 39 L70 39 Q71 39 71 40 L71 50 Q71 51 70 51 L30 51 Q29 51 29 50 L29 40 Q29 39 30 39 Z"/>' +
        '<rect x="19" y="27" width="62" height="5.5" rx="2" fill="#d00000"/><circle cx="79" cy="30" r="2.6" fill="#a00000"/>' +
        '<g class="sk-tails" fill="#d00000"><path d="M79 28 Q89 26 97 32 Q91 32 87 31 Z"/><path d="M79 31 Q87 34 93 42 Q87 39 82 34 Z"/></g>',
      noAntenna: true
    },
    pirate: {
      pal: { a: '#7c2d12', a2: '#451a03' },
      head: '<path d="M12 24 Q30 6 50 8 Q70 6 88 24 Q70 18 50 22 Q30 18 12 24 Z" fill="#111827" stroke="#d4a017" stroke-width=".8"/>' +
        '<path d="M28 16 Q50 -10 72 16 Q50 10 28 16 Z" fill="#111827"/><circle cx="50" cy="7" r="2.8" fill="#fff"/>' +
        '<path d="M46.5 11.5 l7 -2.2 M46.5 9.3 l7 2.2" stroke="#fff" stroke-width=".9" stroke-linecap="round"/>' +
        '<path d="M20 37 L33.5 42.5 M45.5 41 L78 29" stroke="#111" stroke-width="1.3"/><ellipse cx="39.5" cy="44.5" rx="6.6" ry="6.2" fill="#111"/>',
      top: '<g class="sk-parrot"><ellipse cx="83" cy="56" rx="5" ry="7" fill="#16a34a"/><circle cx="84" cy="47" r="4.5" fill="#22c55e"/>' +
        '<path d="M87.5 46 L91 48 L87.3 49.5 Z" fill="#f59e0b"/><circle cx="85" cy="46" r="1" fill="#111"/><path d="M81 62 L78 70 L83 66 Z" fill="#dc2626"/></g>',
      noAntenna: true
    },
    astronaut: {
      pal: { s: '#ffffff', s2: '#e2e8f0', s3: '#94a3b8', a: '#f97316', a2: '#c2410c', v: '#0b1a33', l: '#7dd3fc' },
      back: '<rect x="25" y="57" width="50" height="32" rx="6" fill="#cbd5e1" stroke="#94a3b8" stroke-width=".8"/>',
      head: '<circle cx="50" cy="35" r="33" fill="rgba(186,230,253,.16)" stroke="rgba(255,255,255,.9)" stroke-width="1.6"/>' +
        '<path d="M28 22 Q36 10 50 8" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" opacity=".8"/>',
      body: '<rect x="31.5" y="61" width="5" height="3.4" fill="#1d4ed8"/><rect x="31.5" y="64.4" width="5" height="3.4" fill="#ef4444"/>',
      fx: 'stars'
    },
    superhero: {
      pal: { s: '#e7f0ff', s2: '#9ebcf5', s3: '#3456a3', a: '#e03131', a2: '#a51111' },
      back: '<path class="sk-cape" d="M31 58 Q12 84 16 106 Q33 100 50 106 Q67 100 84 106 Q88 84 69 58 Z" fill="#e03131"/>',
      head: '<path fill-rule="evenodd" fill="#0b1020" stroke="#e03131" stroke-width=".8" d="M29 41 Q34 36.5 44 38.5 Q50 40.5 56 38.5 Q66 36.5 71 41 Q72 48 66 51 Q58 52 50 47.5 Q42 52 34 51 Q28 48 29 41 Z ' +
        'M34.9 44.5 a4.6 5 0 1 0 9.2 0 a4.6 5 0 1 0 -9.2 0 Z M55.9 44.5 a4.6 5 0 1 0 9.2 0 a4.6 5 0 1 0 -9.2 0 Z"/>',
      body: STAR(50, 72.5, 1.25, '#facc15'),
      css: '.sk-superhero .fb-lamp,.sk-superhero .fb-lamp-glow{opacity:0}'
    },
    wizard: {
      pal: { s: '#ece9ff', s2: '#bdb4f6', s3: '#5b4fb3', a: '#3b2a8a', a2: '#241866', l: '#c4f1ff' },
      head: '<ellipse cx="50" cy="20" rx="33" ry="5" fill="#2e1f73"/>' +
        '<path d="M27 19 Q36 -6 44 -18 Q48 -26 58 -24 Q50 -18 54 -6 Q60 8 73 19 Z" fill="#2e1f73"/>' +
        STAR(45, 4, .5, '#facc15') + STAR(51, -10, .35, '#facc15') + '<circle cx="57" cy="10" r="1.3" fill="#facc15"/><circle cx="39" cy="13" r="1" fill="#facc15"/>' +
        '<path d="M31 51.5 Q50 58.5 69 51.5 Q70 63 61 70 Q56 81 50 89 Q44 81 39 70 Q30 63 31 51.5 Z" fill="#f8fafc" stroke="#cbd5e1" stroke-width=".6"/>' +
        '<path d="M42 60 Q44 68 47 74 M58 60 Q56 68 53 74 M50 62 L50 80" fill="none" stroke="#dbe2ea" stroke-width=".9" stroke-linecap="round"/>' +
        '<path d="M40 54.2 Q45 51.6 50 54 Q55 51.6 60 54.2 Q55 57 50 55.6 Q45 57 40 54.2 Z" fill="#fff" stroke="#cbd5e1" stroke-width=".5"/>',
      noAntenna: true, fx: 'stars'
    },

    // ───── Animals ─────
    cat: {
      pal: { s: '#ffe8cc', s2: '#ffc078', s3: '#d9480f', a: '#e8590c', a2: '#b8410a', v: '#1b1206', l: '#b2f2bb' },
      back: '<path class="sk-tail" d="M66 90 Q86 96 88 80 Q90 68 82 62" fill="none" stroke="#e8590c" stroke-width="5" stroke-linecap="round"/>',
      headBack: '<path d="M24 26 L26 4 L42 16 Z" fill="#e8590c"/><path d="M27.5 20 L28.5 9 L37 16 Z" fill="#ffc9c9"/>' +
        '<path d="M76 26 L74 4 L58 16 Z" fill="#e8590c"/><path d="M72.5 20 L71.5 9 L63 16 Z" fill="#ffc9c9"/>',
      head: '<path d="M44 15 L46 22 M50 13.5 L50 21 M56 15 L54 22" stroke="#b8410a" stroke-width="1.6" stroke-linecap="round"/>' +
        '<path d="M48.2 47.4 L51.8 47.4 L50 49.3 Z" fill="#ff8fab"/>' + WHISKERS('#fff'),
      noAntenna: true
    },
    lion: {
      pal: { s: '#ffe7a3', s2: '#f4c34a', s3: '#a8741a', a: '#e0a526', a2: '#a8741a', v: '#2a1a05', l: '#fff3bf' },
      back: '<g class="sk-tail"><path d="M66 90 Q88 98 90 84" fill="none" stroke="#e0a526" stroke-width="3" stroke-linecap="round"/><ellipse cx="90" cy="82" rx="3.5" ry="5" fill="#92400e"/></g>',
      headBack: mane() + '<circle cx="30" cy="15" r="6" fill="#e0a526"/><circle cx="30" cy="15" r="3" fill="#f59f00"/><circle cx="70" cy="15" r="6" fill="#e0a526"/><circle cx="70" cy="15" r="3" fill="#f59f00"/>',
      head: '<path d="M47.5 47 L52.5 47 L50 49.6 Z" fill="#7c2d12"/>' + WHISKERS('#fff3bf'),
      noAntenna: true
    },
    panda: {
      pal: { s: '#ffffff', s2: '#e9ecef', s3: '#495057', a: '#f8f9fa', a2: '#ced4da', v: '#f1f3f5', l: '#ffffff' },
      back: '<g><rect x="16" y="66" width="4" height="36" rx="2" fill="#40c057"/><path d="M16 78 h4 M16 90 h4" stroke="#2b8a3e" stroke-width="1"/>' +
        '<path d="M20 72 q8 -4 10 -10 q-7 2 -10 8" fill="#69db7c"/></g>',
      headBack: '<circle cx="26" cy="16" r="8.5" fill="#212529"/><circle cx="74" cy="16" r="8.5" fill="#212529"/>',
      visor: '<ellipse cx="39.5" cy="45" rx="7" ry="6" transform="rotate(-15 39.5 45)" fill="#212529"/>' +
        '<ellipse cx="60.5" cy="45" rx="7" ry="6" transform="rotate(15 60.5 45)" fill="#212529"/><path d="M48 49 L52 49 L50 51 Z" fill="#212529"/>',
      noAntenna: true,
      css: '.sk-panda .fb-mouth rect{fill:#212529}.sk-panda .fb-arm-l>*,.sk-panda .fb-arm-r>rect,.sk-panda .fb-arm-r>circle{fill:#343a40}'
    },
    bunny: {
      pal: { s: '#fff5f8', s2: '#fcc2d7', s3: '#c2255c', a: '#f783ac', a2: '#d6336c', v: '#2b0a18', l: '#ffd8e8' },
      back: '<circle cx="69" cy="90" r="5.5" fill="#fff" stroke="#fcc2d7"/>',
      headBack: '<g class="sk-ear-l"><ellipse cx="38" cy="2" rx="6.5" ry="17" transform="rotate(-12 38 2)" fill="#fff5f8" stroke="#fcc2d7"/>' +
          '<ellipse cx="38" cy="3" rx="3.2" ry="12" transform="rotate(-12 38 3)" fill="#ffc9de"/></g>' +
        '<g class="sk-ear-r"><ellipse cx="62" cy="2" rx="6.5" ry="17" transform="rotate(12 62 2)" fill="#fff5f8" stroke="#fcc2d7"/>' +
          '<ellipse cx="62" cy="3" rx="3.2" ry="12" transform="rotate(12 62 3)" fill="#ffc9de"/></g>',
      head: '<path d="M48.3 47.6 L51.7 47.6 L50 49.4 Z" fill="#f06595"/>' +
        '<rect x="47.6" y="53" width="2.2" height="3.4" rx=".6" fill="#fff"/><rect x="50.2" y="53" width="2.2" height="3.4" rx=".6" fill="#fff"/>' + WHISKERS('#fce4ec'),
      noAntenna: true, fx: 'hearts'
    }
  };

  // Particles: glyph + colours
  var FX = {
    snow: { g: ['❄', '•', '❅'], c: ['#ffffff', '#e0f2fe'], fall: true },
    bats: { g: ['bat'], c: ['#2b2140', '#4c1d95'] },
    stars: { g: ['✦', '✧', '·'], c: ['#c4b5fd', '#fde68a', '#a5f3fc'] },
    gold: { g: ['✦', '✧', '•'], c: ['#f5c542', '#ffe08a', '#d4a017'] },
    confetti: { g: ['▬', '■', '●'], c: ['#ec4899', '#22c55e', '#facc15', '#3b82f6', '#a855f7'], fall: true },
    gm: { g: ['▬', '■'], c: ['#ce1126', '#0c1c8c', '#3a7728', '#ffffff'], fall: true },
    hearts: { g: ['♥'], c: ['#f06595', '#faa2c1'] }
  };
  var BAT = '<svg viewBox="0 0 24 12" width="14" height="7" aria-hidden="true"><path fill="currentColor" d="M12 3 L13.5 0.5 L14 3.5 Q17 1 24 2 Q21 4 21 8 Q18 6 15 9 Q13.5 7.5 12 11 Q10.5 7.5 9 9 Q6 6 3 8 Q3 4 0 2 Q7 1 10 3.5 L10.5 0.5 Z"/></svg>';

  function fx(type) {
    var f = FX[type]; if (!f) return '';
    var h = '<div class="fit-fx fx-' + type + (f.fall ? ' fx-fall' : ' fx-float') + '" aria-hidden="true">';
    for (var i = 0; i < 9; i++) {
      var g = f.g[i % f.g.length], c = f.c[i % f.c.length];
      var style = '--x:' + (4 + (i * 37) % 92) + '%;--d:' + (3.2 + (i * 7) % 5 * .6).toFixed(1) + 's;--w:' + ((i * 0.7) % 4).toFixed(1) + 's;--s:' + (0.7 + (i % 3) * .25) + ';color:' + c;
      h += '<i style="' + style + '">' + (g === 'bat' ? BAT : g) + '</i>';
    }
    return h + '</div>';
  }

  var css = '';
  Object.keys(L).forEach(function (k) { if (L[k].css) css += L[k].css; });
  if (css) { var st = document.createElement('style'); st.id = 'fitSkinCss'; st.textContent = css; document.head.appendChild(st); }

  window.FitSkins = { layers: L, fx: fx, get: function (id) { return L[id] || L.classic; } };
})();
