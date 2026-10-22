// Videó-építő az EffectCraft After Effects-szerű szkript-objektummodelljére (effectcraft-cli script).
// A Python (app/tools/video.py) elé teszi:  var D = {…};  — minden méret, idő, szöveg és fájlnév onnan jön,
// itt csak a felépítés van. Szerkezet (a szerkesztőben is így látszik):
//   „Video” fő kompozíció: jelenetenként egy beágyazott kompozíció (Title · Scene 1…n · End), áttűnéssel
//   egymásba csúsztatva; fölöttük a feliratok (Caption …, sávval); alul a zene / a saját hang.
//   Jelenet: kép Ken Burns-mozgással (vagy színátmenet, ha nincs kép) · sötét átmenet alul · akcentus-csík ·
//   cím (beúszik) · opcionális nagy szám (stat).
//   D.base (overlay): a felhasználó felvétele az alapsáv a jelenetek helyett. D.layers: időkódos rétegek (rétegenként egy
//   „Layer L1 – …” kompozíció) abszolút időben a fő idővonalon — a make- és az overlay-videón ugyanígy.
//   D.alpha: csak a grafika (felvétel, képek, sötétítés, hang nélkül) — az átlátszó (rgba) renderhez.

function tp(layer) { return layer.property("ADBE Transform Group"); }
function ease(prop) {   // lágy indulás/érkezés minden kulcskockán (ha a motor nem tudja, marad lineáris)
  try {
    for (var k = 1; k <= prop.numKeys; k++)
      prop.setTemporalEaseAtKey(k, [new KeyframeEase(0, 60)], [new KeyframeEase(0, 60)]);
  } catch (e) {}
  // a helyzet EGYENES úton: az automatikus térbeli ív két azonos kulcs közt is elkanyarodik (10-17: az osztott képen
  // a felvétel kitartás közben 26 px-t vándorolt)
  try {
    if (prop.isSpatial) for (var q = 1; q <= prop.numKeys; q++) prop.setSpatialTangentsAtKey(q, [0, 0, 0], [0, 0, 0]);
  } catch (e) {}
}
function textLayer(comp, box, str, o) {
  var t = comp.layers.addBoxText(box, str);
  var p = t.property("ADBE Text Properties").property("ADBE Text Document");
  var d = p.value;
  d.font = o.font || D.font.bold; d.fontSize = o.size; d.fillColor = o.color || [1, 1, 1];
  d.justification = o.align === "center" ? ParagraphJustification.CENTER_JUSTIFY
    : o.align === "right" ? ParagraphJustification.RIGHT_JUSTIFY : ParagraphJustification.LEFT_JUSTIFY;
  if (o.tracking) d.tracking = o.tracking;
  if (o.leading) { try { d.autoLeading = false; d.leading = o.leading; } catch (e) {} }
  p.setValue(d);
  tp(t).property("ADBE Position").setValue(o.pos);
  if (o.name) t.name = o.name;
  if (o.shadow) {
    var sh = t.property("ADBE Effect Parade").addProperty("Drop Shadow");
    sh.property("Softness").setValue(o.shadow); sh.property("Opacity").setValue(60); sh.property("Distance").setValue(4);
  }
  return t;
}
// jelenetváltás (D.transitions): a belépő jelenet (L) a t-től d hosszan jelenik meg az előző (prev) fölött — mozgással,
// vagy az EffectCraft átmeneti effektjével (Transition Completion / Completion: 100 → 0)
var TRANS_FX = { wipe: ["Linear Wipe", { "Wipe Angle": 270, "Feather": 0 }], "wipe-up": ["Linear Wipe", { "Wipe Angle": 0 }],
  clock: ["Radial Wipe", {}], iris: ["CC Radial ScaleWipe", {}], blinds: ["Venetian Blinds", {}],
  blocks: ["Block Dissolve", { "Block Width": 24, "Block Height": 24 }], cards: ["Card Wipe", {}], grid: ["CC Grid Wipe", {}],
  jaws: ["CC Jaws", {}], light: ["CC Light Wipe", {}], twister: ["CC Twister", {}], sweep: ["CC Line Sweep", {}] };
function transIn(comp, L, prev, t, d, kind) {
  var T = tp(L), O = T.property("ADBE Opacity"), P = T.property("ADBE Position"), S = T.property("ADBE Scale"), home = P.value;
  if (TRANS_FX[kind]) {
    try {
      var fx = L.property("ADBE Effect Parade").addProperty(TRANS_FX[kind][0]), c = fx.property(1);
      for (var nm in TRANS_FX[kind][1]) { try { fx.property(nm).setValue(TRANS_FX[kind][1][nm]); } catch (e) {} }
      c.setValueAtTime(t, 100); c.setValueAtTime(t + d, 0); ease(c);
      return;
    } catch (e) { writeLn("transition " + kind + ": " + e + " — fade instead"); }
    kind = "fade";
  }
  var dx = kind.indexOf("-up") > 0 ? 0 : D.W, dy = kind.indexOf("-up") > 0 ? D.H : 0;
  if (kind === "slide" || kind === "slide-up" || kind === "push" || kind === "push-up") {
    P.setValueAtTime(t, [home[0] + dx, home[1] + dy]); P.setValueAtTime(t + d, home); ease(P);
    if (kind.indexOf("push") === 0 && prev) {
      var PP = tp(prev).property("ADBE Position"), ph = PP.value;
      PP.setValueAtTime(t, ph); PP.setValueAtTime(t + d, [ph[0] - dx, ph[1] - dy]); ease(PP);
    }
  } else if (kind === "zoom") {
    S.setValueAtTime(t, [130, 130]); S.setValueAtTime(t + d, [100, 100]); ease(S);
    O.setValueAtTime(t, 0); O.setValueAtTime(t + d * 0.6, 100);
  } else if (kind === "dip") {                    // feketén át: az előző elsötétül, az új a sötétből jön elő
    var blk = comp.layers.addSolid([0, 0, 0], "Dip to black", D.W, D.H, 1);
    blk.moveBefore(L); blk.inPoint = t; blk.outPoint = t + d;
    var BO = tp(blk).property("ADBE Opacity");
    BO.setValueAtTime(t, 0); BO.setValueAtTime(t + d * 0.5, 100); BO.setValueAtTime(t + d, 0);
    O.setValueAtTime(t + d * 0.5 - 0.001, 0); O.setValueAtTime(t + d * 0.5, 100);
  } else { O.setValueAtTime(t, 0); O.setValueAtTime(t + d, 100); }
}
function rect(comp, name, size, pos, color, opacity, round, stroke, strokeW) {
  var s = comp.layers.addShape(); s.name = name;
  var g = s.property("ADBE Root Vectors Group").addProperty("ADBE Vector Group");
  var r = g.property("ADBE Vectors Group").addProperty("ADBE Vector Shape - Rect");
  r.property("ADBE Vector Rect Size").setValue(size);
  if (round) r.property("ADBE Vector Rect Roundness").setValue(round);
  if (color) {
    var f = g.property("ADBE Vectors Group").addProperty("ADBE Vector Graphic - Fill");
    f.property("ADBE Vector Fill Color").setValue([color[0], color[1], color[2], 1]);
  }
  if (stroke) {
    var st = g.property("ADBE Vectors Group").addProperty("ADBE Vector Graphic - Stroke");
    st.property("ADBE Vector Stroke Color").setValue([stroke[0], stroke[1], stroke[2], 1]);
    st.property("ADBE Vector Stroke Width").setValue(strokeW || 4);
  }
  tp(s).property("ADBE Position").setValue(pos);
  if (opacity !== undefined && opacity !== 100) tp(s).property("ADBE Opacity").setValue(opacity);
  return s;
}
function fadeIn(layer, t0, dur, rise) {     // átlátszóság 0→100 (és opcionálisan felúszás) t0-tól dur alatt
  var o = tp(layer).property("ADBE Opacity");
  o.setValueAtTime(t0, 0); o.setValueAtTime(t0 + dur, 100); ease(o);
  if (rise) {
    var p = tp(layer).property("ADBE Position"), v = p.value;
    p.setValueAtTime(t0, [v[0], v[1] + rise]); p.setValueAtTime(t0 + dur, [v[0], v[1]]); ease(p);
  }
}
var FOOT = {};
function picture(comp, file, mo, t0, t1, fadeIn) {   // egy háttérkép a [t0, t1] szakaszra, a saját mozgásával
  var f = FOOT[file] || (FOOT[file] = app.project.importFile(new ImportOptions(new File(file))));
  if (mo.fit === "contain") {     // az egész kép látszik; mögötte önmaga elmosva, sötétebben tölti ki a keretet
    var B = comp.layers.add(f); B.name = "Picture fill";
    if (t0 > 0 || t1 < comp.duration) { B.inPoint = t0; B.outPoint = t1; }
    var sb = Math.max(D.W / f.width, D.H / f.height) * 112;
    tp(B).property("ADBE Scale").setValue([sb, sb]);
    blurred(B, Math.min(D.W, D.H) * 0.06);
    var BO = tp(B).property("ADBE Opacity");
    if (fadeIn) { BO.setValueAtTime(t0, 0); BO.setValueAtTime(t0 + fadeIn, 55); } else BO.setValue(55);
    var Lf = comp.layers.add(f); Lf.name = "Picture";
    if (t0 > 0 || t1 < comp.duration) { Lf.inPoint = t0; Lf.outPoint = t1; }
    var sf = Math.min(D.W / f.width, D.H / f.height) * 100 * 0.96, zf_ = mo.zoom || 1.06;
    var SF = tp(Lf).property("ADBE Scale");
    var mz = mo.motion === "kb-out" || mo.motion === "out";
    SF.setValueAtTime(t0, mz ? [sf * zf_, sf * zf_] : [sf, sf]); SF.setValueAtTime(t1, mz ? [sf, sf] : [sf * zf_, sf * zf_]);
    if (fadeIn) { var FO = tp(Lf).property("ADBE Opacity"); FO.setValueAtTime(t0, 0); FO.setValueAtTime(t0 + fadeIn, 100); }
    return Lf;
  }
  var L = comp.layers.add(f); L.name = "Picture";
  if (t0 > 0 || t1 < comp.duration) { L.inPoint = t0; L.outPoint = t1; }
  var s = Math.max(D.W / f.width, D.H / f.height) * 100, zf = mo.zoom || 0, z = s * (zf || 1.12);
  var S = tp(L).property("ADBE Scale"), P = tp(L).property("ADBE Position");
  var m = mo.motion || "in";
  if (m === "kb-in" || m === "kb-out") {
    // Ken Burns: közelítés ÉS úsztatás egyszerre, a kép érdekes pontja (focus, 0–1) a keret közepe felé;
    // a pozíció úgy korlátozva, hogy a kép végig kitöltse a keretet (lineáris skála + lineáris út → közben is)
    var Z = s * (zf || 1.28), fo = mo.focus || [0.5, 0.45], hw = f.width * Z / 200, hh = f.height * Z / 200;
    var px = D.W / 2 - (fo[0] * f.width - f.width / 2) * Z / 100, py = D.H / 2 - (fo[1] * f.height - f.height / 2) * Z / 100;
    px = Math.min(hw, Math.max(D.W - hw, px)); py = Math.min(hh, Math.max(D.H - hh, py));
    var a = [[s, s], [D.W / 2, D.H / 2]], b = [[Z, Z], [px, py]];
    if (m === "kb-out") { var tmp = a; a = b; b = tmp; }
    S.setValueAtTime(t0, a[0]); S.setValueAtTime(t1, b[0]); P.setValueAtTime(t0, a[1]); P.setValueAtTime(t1, b[1]);
  } else if (m === "out") { S.setValueAtTime(t0, [z, z]); S.setValueAtTime(t1, [s, s]); }
  else if (m === "left" || m === "right" || m === "up" || m === "down") {
    // úsztatás: annyira nagyítva, hogy az út végig kitöltse a keretet (a nagyítás erőssége = az út hossza)
    var k = s * (zf || 1.1), sign = m === "left" || m === "up" ? 1 : -1, horiz = m === "left" || m === "right";
    var room = horiz ? (f.width * k / 100 - D.W) / 2 : (f.height * k / 100 - D.H) / 2, d = Math.max(0, room * 0.9);
    S.setValue([k, k]);
    if (horiz) { P.setValueAtTime(t0, [D.W / 2 + sign * d, D.H / 2]); P.setValueAtTime(t1, [D.W / 2 - sign * d, D.H / 2]); }
    else { P.setValueAtTime(t0, [D.W / 2, D.H / 2 + sign * d]); P.setValueAtTime(t1, [D.W / 2, D.H / 2 - sign * d]); }
  } else if (m === "still") { S.setValue([s, s]); }
  else { S.setValueAtTime(t0, [s, s]); S.setValueAtTime(t1, [z, z]); }
  if (fadeIn) { var O = tp(L).property("ADBE Opacity"); O.setValueAtTime(t0, 0); O.setValueAtTime(t0 + fadeIn, 100); }
  return L;
}
function background(comp, sc, dur) {
  if (D.alpha) return;                     // átlátszó változat: a kép / háttér kimarad
  if (sc.pics && sc.pics.length > 1) {     // képsor: a jelenet hátterei egymás után, áttűnéssel, mindegyik a saját mozgásával
    var n = sc.pics.length, sl = dur / n, xf = Math.min(0.7, sl * 0.3);
    for (var k = 0; k < n; k++) {
      var a0 = k === 0 ? 0 : k * sl - xf / 2, a1 = k === n - 1 ? dur : (k + 1) * sl + xf / 2;
      var L = picture(comp, sc.pics[k].file, { motion: sc.pics[k].motion, focus: sc.pics[k].focus, zoom: sc.zoom, fit: sc.pics[k].fit }, a0, a1, k ? xf : 0);
      if (sc.blur) blurred(L, sc.blur);
    }
  } else if (sc.image) {
    var L1 = picture(comp, sc.image, { motion: sc.motion, focus: sc.focus, zoom: sc.zoom, fit: sc.fit }, 0, dur, 0);
    if (sc.blur) blurred(L1, sc.blur);
  } else if (sc.bg === "dark" || (sc.bg && sc.bg.charAt(0) === "#")) {   // egyszínű háttér
    var c = sc.bg === "dark" ? D.dark : [parseInt(sc.bg.substr(1, 2), 16) / 255, parseInt(sc.bg.substr(3, 2), 16) / 255, parseInt(sc.bg.substr(5, 2), 16) / 255];
    comp.layers.addSolid(c, "Background", D.W, D.H, 1, dur);
  } else {   // nincs kép: két akcentus-tónusú lágy színátmenet („motion”: a fény lassan vándorol, két elmosott folt úszik)
    var g = comp.layers.addSolid(D.dark, "Background", D.W, D.H, 1, dur);
    var fx = g.property("ADBE Effect Parade").addProperty("Gradient Ramp");
    var st = fx.property("Start of Ramp"), en = fx.property("End of Ramp");
    st.setValue([0, 0]); fx.property("Start Color").setValue([D.accent[0] * 0.55, D.accent[1] * 0.55, D.accent[2] * 0.55]);
    en.setValue([D.W, D.H]); fx.property("End Color").setValue(D.dark);
    if (sc.bg === "motion") {
      st.setValueAtTime(0, [0, 0]); st.setValueAtTime(dur, [D.W * 0.35, D.H * 0.2]);
      en.setValueAtTime(0, [D.W, D.H]); en.setValueAtTime(dur, [D.W * 0.75, D.H]);
      var mm = Math.min(D.W, D.H);
      for (var b = 0; b < 2; b++) {
        var r = mm * (b ? 0.55 : 0.75), blob = rect(comp, "Glow " + (b + 1), [r, r], [D.W * (b ? 0.78 : 0.22), D.H * (b ? 0.7 : 0.3)],
          b ? [D.accent[0] * 0.6, D.accent[1] * 0.6, D.accent[2] * 0.6] : D.accent, b ? 16 : 12, r / 2);
        blurred(blob, mm * 0.12);
        var BP = tp(blob).property("ADBE Position"), p0 = BP.value;
        BP.setValueAtTime(0, p0); BP.setValueAtTime(dur, [p0[0] + (b ? -1 : 1) * D.W * 0.12, p0[1] + (b ? -1 : 1) * D.H * 0.06]);
      }
    }
  }
}
function blurred(L, amount) {
  var b = L.property("ADBE Effect Parade").addProperty("Gaussian Blur");
  b.property("Blurriness").setValue(amount); b.property("Repeat Edge Pixels").setValue(1);
}
function camera(L, keys, t0, f) {   // a jelenet (vagy a saját felvétel) virtuális kamerája: [[t, nagyítás, x, y]]; f: a réteg
  var S = tp(L).property("ADBE Scale"), P = tp(L).property("ADBE Position"), q = (f || 1) * 100;   // alapnagyítása
  for (var i = 0; i < keys.length; i++) {
    var k = keys[i];
    S.setValueAtTime(t0 + k[0], [k[1] * q, k[1] * q]); P.setValueAtTime(t0 + k[0], [k[2], k[3]]);
  }
  ease(S); ease(P);
}
var SHADES = {};
function shade(comp, dur, kind, scrim) {   // alul sötétedő átmenet: előre megrajzolt PNG (olcsó); „even” = egyenletes sötétítés
  // scrim (0–1): a sötétítés erőssége — 0 = semmi (10-19: backdrop:0 mellett is sötét maradt a jelenet); nincs = a beépített
  if (D.alpha || scrim === 0) return null;
  var own = scrim !== undefined && scrim !== null;
  if (kind === "even" || !D.shade || !D.shade[kind]) {
    var s = comp.layers.addSolid([0, 0, 0], "Shade", D.W, D.H, 1, dur);
    tp(s).property("ADBE Opacity").setValue(own ? scrim * 100 : kind === "even" ? 70 : 60);
    return s;
  }
  SHADES[kind] = SHADES[kind] || app.project.importFile(new ImportOptions(new File(D.shade[kind])));
  var L = comp.layers.add(SHADES[kind]); L.name = "Shade";
  if (own) tp(L).property("ADBE Opacity").setValue(Math.min(100, scrim / 0.9 * 100));   // a PNG maga ~0,9-ig sötétedik
  return L;
}
function backdropFor(comp, t, name) {   // sötét, lekerekített alap egy szöveg mögé (a tényleges, kirajzolt méretére)
  try {
    var r = t.sourceRectAtTime(0, false), P = tp(t).property("ADBE Position").value, pad = Math.max(6, r.height * 0.45);
    var b = rect(comp, name, [r.width + pad * 2.4, r.height + pad * 2], [P[0] + r.left + r.width / 2, P[1] + r.top + r.height / 2],
      D.dark, 74, (r.height + pad * 2) / 2);
    b.moveAfter(t);
    return b;
  } catch (e) { return null; }
}
function accentBar(comp, x, y, t0, center) {
  var w = D.L.bar[0], h = D.L.bar[1];
  var b = rect(comp, "Accent bar", [w, h], [center ? x : x + w / 2, y], D.accent, 100, h / 2);
  var sc = tp(b).property("ADBE Scale");
  sc.setValueAtTime(t0, [0, 100]); sc.setValueAtTime(t0 + 0.45, [100, 100]); ease(sc);
  if (!center) {   // balról nő: a horgony a bal szélen
    try { tp(b).property("ADBE Anchor Point").setValue([-w / 2, 0]); tp(b).property("ADBE Position").setValue([x, y]); } catch (e) {}
  }
  return b;
}

function titleScene(sc) {
  var c = app.project.items.addComp("Title", D.W, D.H, 1, sc.dur, D.fps);
  background(c, sc, sc.dur);
  shade(c, sc.dur, "title", sc.scrim);
  var L = D.L;
  if (sc.kicker) {
    var kt = textLayer(c, [L.titleW, L.kickerSize * 1.6], sc.kicker.toUpperCase(),
      { size: L.kickerSize, color: D.accent, align: L.titleAlign, pos: [L.titleX, L.kickerY], tracking: 80, name: "Kicker", font: D.font.semibold });
    fadeIn(kt, 0.2, 0.5, 0);
    // a piros felcím a fotón sötét alapon (10-19: a képen alig volt olvasható)
    var kb = (sc.image || sc.pics) && !D.alpha ? backdropFor(c, kt, "Kicker backdrop") : null;
    if (kb) { var ko = tp(kb).property("ADBE Opacity"); ko.setValueAtTime(0.2, 0); ko.setValueAtTime(0.7, 74); ease(ko); }
  }
  accentBar(c, L.titleAlign === "center" ? D.W / 2 : L.titleX - L.titleW / 2, L.barY, 0.15, L.titleAlign === "center");
  fadeIn(textLayer(c, [L.titleW, L.titleH], sc.text, { size: sc.size, align: L.titleAlign, pos: [L.titleX, L.titleY],
    leading: sc.size * 1.08, shadow: 24, name: "Title" }), 0.35, 0.7, D.H * 0.025);
  return c;
}
function grower(layer, el) {   // kinövés: a horgony a növekedés kezdő élén, a skála 0 → 100 (t0-tól dur alatt)
  var w = el.size[0], h = el.size[1], T = tp(layer), S = T.property("ADBE Scale"), g = el.grow;
  var A = { up: [0, h / 2], down: [0, -h / 2], right: [-w / 2, 0], left: [w / 2, 0], segment: [-w / 2, 0] }[g];
  if (A) {
    try { T.property("ADBE Anchor Point").setValue(A); } catch (e) {}
    var P = el.pos;
    if (g !== "segment") P = [el.pos[0] + A[0], el.pos[1] + A[1]];   // a horgonypont helye a kompozícióban
    T.property("ADBE Position").setValue(P);
    var z = g === "up" || g === "down" ? [100, 0] : [0, 100];
    S.setValueAtTime(el.t0, z); S.setValueAtTime(el.t0 + el.dur, [100, 100]); ease(S);
  } else if (g === "rotate") {      // mérőóra-mutató: a horgony a tövénél, elfordul rot0 → rot
    try { T.property("ADBE Anchor Point").setValue([-w / 2, 0]); } catch (e) {}
    T.property("ADBE Position").setValue(el.pos);
    var R = T.property("ADBE Rotate Z");
    R.setValueAtTime(el.t0, el.rot0); R.setValueAtTime(el.t0 + el.dur, el.rot); ease(R);
    var Op = T.property("ADBE Opacity");          // beúszik (az áttűnés alatt ne álljon ott egyedül a skála előtt)
    Op.setValueAtTime(Math.max(0, el.t0 - 0.3), 0); Op.setValueAtTime(el.t0, 100);
  } else if (g === "center") {      // középről nyílik (tölcsér)
    S.setValueAtTime(el.t0, [0, 100]); S.setValueAtTime(el.t0 + el.dur, [100, 100]); ease(S);
  } else if (g === "pop") {
    S.setValueAtTime(el.t0, [0, 0]); S.setValueAtTime(el.t0 + el.dur, [100, 100]); ease(S);
  } else if (el.t0 !== undefined) {
    var o = T.property("ADBE Opacity"), full = el.opacity || 100;
    o.setValueAtTime(el.t0, 0); o.setValueAtTime(el.t0 + el.dur, full);
  }
}
function keyed(layer, el) {      // kulcskockák [[t, x, y, szélesség?, átlátszóság?]]: helyzet (téglalapnál a bal széle),
  var T = tp(layer), P = T.property("ADBE Position"), S = T.property("ADBE Scale"), O = T.property("ADBE Opacity");   // szélesség, láthatóság
  var w0 = el.k === "rect" ? el.size[0] : 0;
  if (w0) { try { T.property("ADBE Anchor Point").setValue([-w0 / 2, 0]); } catch (e) {} }
  for (var i = 0; i < el.keys.length; i++) {
    var k = el.keys[i];
    P.setValueAtTime(k[0], [k[1], k[2]]);
    if (w0 && k[3] !== undefined && k[3] !== null) S.setValueAtTime(k[0], [Math.max(0.1, k[3] / w0 * 100), 100]);
    if (k[4] !== undefined && k[4] !== null) O.setValueAtTime(k[0], k[4]);
  }
  ease(P); if (w0) ease(S);
}
function col(v) { return v === "accent" ? D.accent : v; }
function pathLayer(comp, el) {   // vonal / nyíl / útvonal / sokszög: a pontok KOMPOZÍCIÓ-pixelben (a réteg a [0,0]-ban)
  var s = comp.layers.addShape(); s.name = el.name;
  tp(s).property("ADBE Position").setValue([0, 0]);
  var g = s.property("ADBE Root Vectors Group").addProperty("ADBE Vector Group");
  var p = g.property("ADBE Vectors Group").addProperty("ADBE Vector Shape - Group");
  var sh = new Shape(); sh.vertices = el.points; sh.closed = !!el.closed;
  p.property("ADBE Vector Shape").setValue(sh);
  if (el.fill) {
    var f = g.property("ADBE Vectors Group").addProperty("ADBE Vector Graphic - Fill"), fc = col(el.fill);
    f.property("ADBE Vector Fill Color").setValue([fc[0], fc[1], fc[2], 1]);
  }
  if (el.stroke) {
    var st = g.property("ADBE Vectors Group").addProperty("ADBE Vector Graphic - Stroke"), sc_ = col(el.stroke);
    st.property("ADBE Vector Stroke Color").setValue([sc_[0], sc_[1], sc_[2], 1]);
    st.property("ADBE Vector Stroke Width").setValue(el.stroke_w || 6);
    try { st.property("ADBE Vector Stroke Line Cap").setValue(2); st.property("ADBE Vector Stroke Line Join").setValue(2); } catch (e) {}
    if (el.dash) {                 // szaggatott / pontozott (kerek véggel a rövid vonás pötty): [vonás, köz] px
      try { var ds = st.property("ADBE Vector Stroke Dashes"); ds.property(1).setValue(el.dash[0]); ds.property(2).setValue(el.dash[1]); } catch (e) {}
    }
  }
  if (el.opacity !== undefined && el.opacity !== 100) tp(s).property("ADBE Opacity").setValue(el.opacity);
  if (el.anim === "draw") {        // kirajzolódik: Trim Paths vége 0 → 100
    var tr = s.property("ADBE Root Vectors Group").addProperty("ADBE Vector Filter - Trim");
    var e_ = tr.property("ADBE Vector Trim End"); e_.setValueAtTime(el.t0, 0); e_.setValueAtTime(el.t0 + el.dur, 100); ease(e_);
    dims(s, el);
  } else animate(s, el);
  return s;
}
function dims(layer, el) {       // irányított figyelem: a megadott szakaszokban [tól, ig] halványabb, utána vissza
  if (!el.dim || !el.dim.length) return;
  var O = tp(layer).property("ADBE Opacity"), full = el.opacity || 100, lo = full * 0.32;
  for (var i = 0; i < el.dim.length; i++) {
    var a = el.dim[i][0], b = el.dim[i][1];
    O.setValueAtTime(a, full); O.setValueAtTime(a + 0.25, lo); O.setValueAtTime(b, lo); O.setValueAtTime(b + 0.35, full);
  }
}
function animate(layer, el) {    // a saját grafika mozgásai: appear | pop | grow | slide-* | pulse | none (+ move_to)
  var T = tp(layer), a = el.anim || "appear", t0 = el.t0 || 0, d = el.dur || 0.6, full = el.opacity || 100;
  var O = T.property("ADBE Opacity"), S = T.property("ADBE Scale"), P = T.property("ADBE Position"), home = P.value;
  var s0 = S.value[0];             // a doboz méretére illesztett kép / videó saját mérete (mediaLayer) — a pop ide nő, nem 100%-ra
  if (a === "appear" || a === "grow" || a.indexOf("slide-") === 0) { O.setValueAtTime(t0, 0); O.setValueAtTime(t0 + Math.min(d, 0.5), full); }
  if (a === "pop" || a === "grow") { S.setValueAtTime(t0, [0, 0]); S.setValueAtTime(t0 + d, [s0, s0]); ease(S); }
  if (a.indexOf("slide-") === 0) {
    var p = P.value, off = Math.min(D.W, D.H) * 0.12;
    var dx = a === "slide-left" ? off : a === "slide-right" ? -off : 0, dy = a === "slide-up" ? off : a === "slide-down" ? -off : 0;
    P.setValueAtTime(t0, [p[0] + dx, p[1] + dy]); P.setValueAtTime(t0 + d, p); ease(P);
  }
  if (a === "pulse") {
    O.setValueAtTime(t0, 0); O.setValueAtTime(t0 + 0.3, full);
    for (var q = 0; q < 3; q++) { var b = t0 + 0.3 + q * 0.8; S.setValueAtTime(b, [s0, s0]); S.setValueAtTime(b + 0.4, [s0 * 1.12, s0 * 1.12]); S.setValueAtTime(b + 0.8, [s0, s0]); }
    ease(S);
  }
  if (el.move_to) {                // odasiklik: amint megjelent, move_dur alatt (alap: dur), lágyan indul és érkezik
    // (10-19: dur 2.2 mellett 2,4 s várakozás + 4,4 s út — a jelölő 4,3 s-nál még az út elején járt)
    var shown = a === "appear" ? Math.min(d, 0.5) : a === "pulse" ? 0.3 : a === "none" ? 0 : d, tm = t0 + shown;
    P.setValueAtTime(tm, home); P.setValueAtTime(tm + (el.move_dur || d), el.move_to); ease(P);
  }
  dims(layer, el);
}
function multiPath(comp, el) {   // sok útvonal egy rétegen, csoportonként közös vonallal / kitöltéssel (térkép)
  var s = comp.layers.addShape(); s.name = el.name;
  tp(s).property("ADBE Position").setValue([0, 0]);
  var root = s.property("ADBE Root Vectors Group");
  for (var gi = 0; gi < el.groups.length; gi++) {
    var G = el.groups[gi], g = root.addProperty("ADBE Vector Group"); g.name = G.name;
    var vg = g.property("ADBE Vectors Group");
    for (var pi = 0; pi < G.paths.length; pi++) {
      var sh = new Shape(); sh.vertices = G.paths[pi]; sh.closed = !!G.closed;
      vg.addProperty("ADBE Vector Shape - Group").property("ADBE Vector Shape").setValue(sh);
    }
    if (G.fill) { var f = vg.addProperty("ADBE Vector Graphic - Fill"), fc = col(G.fill); f.property("ADBE Vector Fill Color").setValue([fc[0], fc[1], fc[2], 1]); }
    if (G.stroke) {
      var st = vg.addProperty("ADBE Vector Graphic - Stroke"), c_ = col(G.stroke);
      st.property("ADBE Vector Stroke Color").setValue([c_[0], c_[1], c_[2], 1]);
      st.property("ADBE Vector Stroke Width").setValue(G.stroke_w || 2);
      try { st.property("ADBE Vector Stroke Line Cap").setValue(2); st.property("ADBE Vector Stroke Line Join").setValue(2); } catch (e) {}
    }
  }
  if (el.opacity !== undefined && el.opacity !== 100) tp(s).property("ADBE Opacity").setValue(el.opacity);
  if (el.anim === "draw") {        // az utak kirajzolódnak, közben a réteg beúszik
    var tr = root.addProperty("ADBE Vector Filter - Trim");
    var e_ = tr.property("ADBE Vector Trim End"); e_.setValueAtTime(el.t0, 0); e_.setValueAtTime(el.t0 + el.dur, 100); ease(e_);
    var O = tp(s).property("ADBE Opacity"); O.setValueAtTime(el.t0, 0); O.setValueAtTime(el.t0 + 0.4, el.opacity || 100);
  } else animate(s, el);
  return s;
}
function mediaLayer(comp, el) {   // kép vagy Lottie egy dobozba illesztve
  var src = null;
  if (el.k === "lottie") {
    for (var i = 1; i <= app.project.numItems; i++) if (app.project.item(i).name === el.comp) src = app.project.item(i);
  } else {
    src = FOOT[el.file] || (FOOT[el.file] = app.project.importFile(new ImportOptions(new File(el.file))));
  }
  if (!src) return null;
  var L = comp.layers.add(src); L.name = el.name;
  var fx = el.size[0] / src.width, fy = el.size[1] / src.height;
  var k = (el.fit === "cover" ? Math.max(fx, fy) : Math.min(fx, fy)) * 100;   // cover: kitölti a dobozt (a túllógó rész kilóg)
  tp(L).property("ADBE Scale").setValue([k, k]);
  tp(L).property("ADBE Position").setValue(el.pos);
  if (el.k === "lottie") {
    L.startTime = el.t0 || 0;
    if (el.loop !== false) {      // ismétlődik a jelenet végéig: időtérkép + loopOut
      try {
        L.timeRemapEnabled = true;
        L.property("ADBE Time Remapping").expression = 'loopOut("cycle")';
        L.outPoint = comp.duration;
      } catch (e) { writeLn("lottie loop: " + e); }
    }
  }
  if (el.movie) L.startTime = el.t0 || 0;     // bevágott felvétel: a megjelenésekor indul (néma — a hang az alapsávé)
  if (el.opacity !== undefined && el.opacity !== 100) tp(L).property("ADBE Opacity").setValue(el.opacity);
  animate(L, el);
  return L;
}
function probeText(t, el) {     // a szöveg KIRAJZOLT doboza (tinta, a réteg horgonyához képest) a Pythonnak: ütközés,
  try {                          // kilógás a dobozából, váratlan tördelés — a check ebből szól (_text_report)
    var r = t.sourceRectAtTime(0, false), q = function (v) { return Math.round(v * 10) / 10; };
    writeLn("TEXT_BOX " + JSON.stringify([el.probe, q(r.left), q(r.top), q(r.width), q(r.height)]));
  } catch (e) {}
}
function chartLayers(c, ch) {   // a Python (chart_elements) számolta geometria: minden oszlop / pont / felirat külön réteg
  var F = { bold: D.font.bold, semibold: D.font.semibold, regular: D.font.regular };
  for (var k = 0; k < ch.elements.length; k++) {
    var el = ch.elements[k];
    if (el.k === "rect") {
      var r = rect(c, el.name, el.size, el.pos, col(el.color), el.opacity, el.round, el.stroke ? col(el.stroke) : null, el.stroke_w);
      if (el.rot && el.grow !== "rotate") tp(r).property("ADBE Rotate Z").setValue(el.rot);
      if (el.keys) keyed(r, el); else if (el.grow) grower(r, el); else if (el.anim) animate(r, el);
    } else if (el.k === "path") {
      pathLayer(c, el);
    } else if (el.k === "multipath") {
      multiPath(c, el);
    } else if (el.k === "image" || el.k === "lottie") {
      mediaLayer(c, el);
    } else {
      var tl = textLayer(c, el.box, el.str, { size: el.size, align: el.align, pos: el.pos, name: el.name,
        font: F[el.font] || D.font.semibold, color: el.color, leading: el.leading, tracking: el.tracking, shadow: el.shadow });
      if (el.probe) probeText(tl, el);
      if (el.keys) keyed(tl, el);
      else if (el.t1 !== undefined || el.cut) {   // felpörgő szám: rövid ideig látható, egymást váltó rétegek (áttűnés nélkül)
        tl.inPoint = el.t0 || 0;
        if (el.t1 !== undefined) tl.outPoint = el.t1;
      } else if (el.anim) animate(tl, el); else fadeIn(tl, el.t0 || 0, 0.4, 0);
    }
  }
}
function runScript(c, sc) {      // az ügynök saját kódja a jelenetre: comp, W, H, dur, accent, D (+ a fenti segédfüggvények)
  try {
    (new Function("comp", "W", "H", "dur", "accent", "D", sc.chart.code))(c, c.width, c.height, sc.dur, D.accent, D);
  } catch (e) { writeLn("SCRIPT_ERROR " + sc.chart.id + ": " + (e && e.message ? e.message : String(e))); }
  var texts = [];                 // a szövegek számait a Python ellenőrzi a forráson
  for (var i = 1; i <= c.numLayers; i++) {
    try { var tp_ = c.layer(i).property("ADBE Text Properties"); if (tp_) texts.push(String(tp_.property("ADBE Text Document").value.text)); } catch (e) {}
  }
  writeLn("SCRIPT_TEXT " + sc.chart.id + " " + JSON.stringify(texts));
}
function contentScene(sc, i) {
  var c = app.project.items.addComp("Scene " + i + " – " + sc.headline.substring(0, 32), D.W, D.H, 1, sc.dur, D.fps);
  // kamera: a kép és az ábra egy belső kompozícióban mozog (legalul), a feliratok (cím, nagy szám) a helyükön maradnak
  // — 10-17: a mezre közelítéskor a jelenet címe kicsúszott a képből
  var host = c;
  if (sc.camera) {
    host = app.project.items.addComp("Scene " + i + " – camera", D.W, D.H, 1, sc.dur, D.fps);
    var Lc = c.layers.add(host); Lc.name = "Camera";
    camera(Lc, sc.camera, 0);
  }
  background(host, sc, sc.dur);
  var L = D.L;
  if (sc.chart) {        // mozgó grafikon: sötétített kép, fent a cím, alatta a grafikon
    shade(host, sc.dur, "even", sc.scrim);
    // + egyenletes sötét fátyol: zsúfolt képen (10-12: EEG-görbe az idővonal mögött) is olvasható maradjon az ábra
    var bd = sc.chart.backdrop === undefined || sc.chart.backdrop === null ? 58 : Math.round(sc.chart.backdrop * 100);
    if (bd > 0 && !D.alpha) rect(host, "Chart backdrop", [D.W, D.H], [D.W / 2, D.H / 2], [0.03, 0.035, 0.05], bd);
    var cs = Math.round(Math.min(D.W, D.H) * 0.052);
    chartLayers(host, sc.chart);
    if (sc.chart.code) runScript(host, sc);
    // a cím az ábra FÖLÖTT (10-12: a teljes képes térkép útjai átfutottak rajta)
    if (sc.chart.title) fadeIn(textLayer(c, [D.W * 0.84, cs * 2.6], sc.chart.title, { size: cs, align: "center", pos: [D.W / 2, D.H * 0.1],
      leading: cs * 1.1, name: "Chart title" }), 0.1, 0.5, D.H * 0.015);
    return c;
  }
  if (sc.stat) {
    shade(c, sc.dur, "even", sc.scrim);
    var ss = sc.stat.size || L.statSize;
    fadeIn(textLayer(c, [D.W * 0.94, ss * 1.3], sc.stat.value, { size: ss, color: D.accent, align: "center",
      pos: [D.W / 2, L.statY + (L.statSize - ss) * 0.4], name: "Number" }), 0.2, 0.6, D.H * 0.03);
    fadeIn(textLayer(c, [D.W * 0.8, L.statLabelSize * 3.2], sc.stat.label, { size: L.statLabelSize, align: "center",
      pos: [D.W / 2, L.statLabelY], name: "Number label", font: D.font.semibold }), 0.5, 0.6, D.H * 0.02);
  } else {
    shade(c, sc.dur, "scene", sc.scrim);
    accentBar(c, L.headX - L.headW / 2, L.headBarY, 0.1, false);
    fadeIn(textLayer(c, [L.headW, L.headH], sc.headline, { size: sc.size, align: "left", pos: [L.headX, L.headY],
      leading: sc.size * 1.12, name: "Headline" }), 0.25, 0.6, D.H * 0.02);
  }
  return c;
}
function endScene(sc) {
  var c = app.project.items.addComp("End", D.W, D.H, 1, sc.dur, D.fps);
  background(c, sc, sc.dur);              // a kép itt előre elmosott (a Python készíti), nincs képkockánkénti blur
  if (sc.image) shade(c, sc.dur, "even", sc.scrim);
  var L = D.L;
  fadeIn(textLayer(c, [D.W * 0.84, L.titleH], sc.title, { size: sc.size, align: "center", pos: [D.W / 2, L.endY],
    leading: sc.size * 1.1, name: "End title" }), 0.2, 0.6, D.H * 0.02);
  accentBar(c, D.W / 2, L.endBarY, 0.4, true);
  if (sc.subtitle) fadeIn(textLayer(c, [D.W * 0.8, L.capSize * 3.4], sc.subtitle, { size: L.capSize, align: "center",
    pos: [D.W / 2, L.endSubY], name: "End subtitle", font: D.font.regular }), 0.6, 0.6, 0);
  if (sc.credits) textLayer(c, [D.W * 0.9, L.creditSize * 4], sc.credits, { size: L.creditSize, color: [0.75, 0.76, 0.8],
    align: "center", pos: [D.W / 2, L.creditY], name: "Picture credits", font: D.font.regular });
  return c;
}

// időkódos réteg: saját kompozíció (a Python számolta elemekkel) a fő idővonalon at-tól dur-ig, be/ki úsztatva;
// osztott képnél az alapsáv (felvétel vagy jelenetek) a fél képre zsugorodik, a végén vissza
function splitBase(Lb, ly) {
  var T = tp(Lb), S = T.property("ADBE Scale"), P = T.property("ADBE Position"), s = ly.split;
  var a = ly.at, b = ly.at + ly.dur, d = Math.min(0.5, ly.dur / 4), k = s.scale * 100 * BASE_K, p0 = P.valueAtTime(a, false), s0 = S.valueAtTime(a, false);
  S.setValueAtTime(a, s0); S.setValueAtTime(a + d, [k, k]); S.setValueAtTime(b - d, [k, k]); S.setValueAtTime(b, s0);
  P.setValueAtTime(a, p0); P.setValueAtTime(a + d, s.to); P.setValueAtTime(b - d, s.to); P.setValueAtTime(b, p0);
  ease(S); ease(P);
}
function waveLayer(c, ly) {    // hullámforma: a hangerő (a Python számolta, WAVE_STEP-enként) egy csúszkán, a sávok követik
  var wv = ly.wave, n = wv.bars, W = ly.w, H = ly.h, gap = W / n, bw = Math.max(3, Math.round(gap * 0.55)), col = wv.color;
  var nl = c.layers.addNull(); nl.name = "Audio Level";
  var sl = nl.property("ADBE Effect Parade").addProperty("Slider Control"); sl.name = "Level";
  var sv = sl.property(1);
  if (!wv.levels.length) sv.setValue(0);
  for (var i = 0; i < wv.levels.length; i++) sv.setValueAtTime(i * wv.step, wv.levels[i]);
  var rise = wv.style === "rise";
  for (var b = 0; b < n; b++) {
    var s = c.layers.addShape(); s.name = "Bar " + (b + 1);
    var g = s.property("ADBE Root Vectors Group").addProperty("ADBE Vector Group");
    var r = g.property("ADBE Vectors Group").addProperty("ADBE Vector Shape - Rect");
    r.property("ADBE Vector Rect Roundness").setValue(bw / 2);
    var f = g.property("ADBE Vectors Group").addProperty("ADBE Vector Graphic - Fill");
    f.property("ADBE Vector Fill Color").setValue([col[0], col[1], col[2], 1]);
    var hx = 'var v = thisComp.layer("Audio Level").effect("Level")("Slider") / 100; var h = Math.max(' + bw + ', ' +
      (rise ? H : H * 0.96) + ' * v * ' + wv.shape[b] + ' * (0.8 + 0.2 * Math.sin(time * 11 + ' + (b * 1.9).toFixed(2) + ')));';
    r.property("ADBE Vector Rect Size").expression = hx + ' [' + bw + ', h]';
    if (rise) r.property("ADBE Vector Rect Position").expression = hx + ' [0, -h / 2]';
    tp(s).property("ADBE Position").setValue([gap * (b + 0.5), rise ? H : H / 2]);
  }
}
function overlayLayers(main, base) {
  for (var i = 0; i < (D.layers || []).length; i++) {
    var ly = D.layers[i];
    var c = app.project.items.addComp(ly.name, ly.w, ly.h, 1, ly.dur, D.fps);
    chartLayers(c, ly);
    if (ly.wave) waveLayer(c, ly);
    if (ly.code) runScript(c, { chart: { code: ly.code, id: ly.id }, dur: ly.dur });
    var L = main.layers.add(c); L.name = ly.name; L.startTime = ly.at;
    var T = tp(L), P = T.property("ADBE Position"), O = T.property("ADBE Opacity"), S = T.property("ADBE Scale");
    var a = ly.at, b = ly.at + ly.dur, di = Math.min(0.45, ly.dur / 4), dq = Math.min(0.35, ly.dur / 4);
    var ai = ly.anim["in"], ao = ly.anim["out"], off = [ly.pos[0] + ly.slide[0], ly.pos[1] + ly.slide[1]];
    P.setValue(ly.pos);
    if (ai !== "none") { O.setValueAtTime(a, 0); O.setValueAtTime(a + di, 100); }
    if (ao !== "none") { O.setValueAtTime(b - dq, 100); O.setValueAtTime(b, 0); }
    if (ai === "slide") { P.setValueAtTime(a, off); P.setValueAtTime(a + di, ly.pos); }
    if (ao === "slide") { P.setValueAtTime(b - dq, ly.pos); P.setValueAtTime(b, off); }
    if (ai === "pop") { S.setValueAtTime(a, [86, 86]); S.setValueAtTime(a + di, [100, 100]); }
    if (ao === "pop") { S.setValueAtTime(b - dq, [100, 100]); S.setValueAtTime(b, [86, 86]); }
    ease(O); ease(P); ease(S);
    if (ly.split) for (var j = 0; j < base.length; j++) splitBase(base[j], ly);
  }
}

var main = app.project.items.addComp("Video", D.W, D.H, 1, D.total, D.fps);
main.bgColor = D.dark;
var baseLayers = [], BASE_K = 1;
if (D.base) {          // overlay: a felhasználó felvétele (képe és hangja) az alapsáv
  if (!D.alpha) {
    var fb = main.layers.add(app.project.importFile(new ImportOptions(new File(D.base.file))));
    fb.name = D.base.label || "Footage"; baseLayers.push(fb);
    if (D.base.scale && D.base.scale !== 1) {   // a munkamásolat (kicsi) a kompozíció méretére: a rétegek a teljes méretben
      BASE_K = D.base.scale;
      tp(fb).property("ADBE Scale").setValue([BASE_K * 100, BASE_K * 100]);
    }
    if (D.base.hold) {   // zárókártya: a felvétel az utolsó kockáján áll (időtérkép; a hang ott csend) — nincs meghosszabbított fájl
      fb.timeRemapEnabled = true;
      var TR = fb.property("ADBE Time Remapping"), last = Math.max(0, D.base.hold - 1 / D.fps);
      while (TR.numKeys > 0) TR.removeKey(1);
      TR.setValueAtTime(0, 0); TR.setValueAtTime(last, last);
      fb.outPoint = D.total;
    }
    if (D.camera) camera(fb, D.camera, 0, BASE_K);      // ráközelítés a felvételre (punch-in), abszolút időben
  }
} else {
  var order = [titleScene(D.title)], starts = [0], t = D.title.dur - D.xfade;
  for (var i = 0; i < D.scenes.length; i++) { order.push(contentScene(D.scenes[i], i + 1)); starts.push(t); t += D.scenes[i].dur - D.xfade; }
  order.push(endScene(D.end)); starts.push(t);
  for (var j = 0; j < order.length; j++) {    // alulról fölfelé: a későbbi jelenet a korábbi FÖLÉ kerül, és beúszik
    var L = main.layers.add(order[j]);
    L.startTime = starts[j];
    if (j > 0) transIn(main, L, baseLayers[j - 1], starts[j], D.xfade, (D.transitions || [])[j - 1] || "fade");
    baseLayers.push(L);
  }
}
overlayLayers(main, baseLayers);      // a rétegek a jelenetek / a felvétel fölött, a feliratok alatt
// feliratok: sáv + szöveg, a jelenetek fölött (a szerkesztőben egyenként átírhatók). Stílus (D.captionStyle): box (alap) |
// karaoke | highlight | reveal — a sávon, szavanként animálva | bold — sáv nélkül, nagy körvonalas betű, 1–3 szó
function wordAnim(t, cp, st, size) {   // szavankénti animátor: a kijelölés (index, szavak) a szavak kezdőidején ugrik
  var a = t.property("ADBE Text Properties").property("ADBE Text Animators").addProperty("ADBE Text Animator");
  var ap = a.property("ADBE Text Animator Properties"), n = cp.words.length, ac = D.accent;
  if (st === "reveal") ap.addProperty("ADBE Text Opacity").setValue(0);
  else ap.addProperty("ADBE Text Fill Color").setValue([ac[0], ac[1], ac[2], 1]);
  if (st === "highlight" || st === "bold") ap.addProperty("ADBE Text Position 3D").setValue([0, -Math.round(size * 0.07), 0]);
  var sel = a.property("ADBE Text Selectors").property(1), adv = sel.property("advanced");
  adv.property("units").setValue(2); adv.property("basedOn").setValue(3);       // index · szavak
  var s = sel.property("start"), e = sel.property("end");
  function key(tm, x, y) { s.setValueAtTime(tm, x); e.setValueAtTime(tm, y); }
  key(cp.t0, 0, st === "reveal" ? n : 0);                                         // a darab elején: semmi / minden rejtve
  for (var i = 0; i < n; i++) {
    var tm = Math.max(cp.t0 + 0.001 * (i + 1), cp.words[i]);
    if (st === "karaoke") key(tm, 0, i + 1);
    else if (st === "reveal") key(tm, i + 1, n);
    else key(tm, i, i + 1);
  }
  for (var kk = 1; kk <= s.numKeys; kk++) {
    s.setInterpolationTypeAtKey(kk, KeyframeInterpolationType.HOLD); e.setInterpolationTypeAtKey(kk, KeyframeInterpolationType.HOLD);
  }
}
var capSt = D.captionStyle || "box";
for (var k = 0; k < (D.captions || []).length; k++) {
  var cp = D.captions[k], Lc = D.L, lays;
  if (capSt === "bold") {
    var bs = cp.size || Math.round(Lc.capSize * 1.5);
    var tb = textLayer(main, [Lc.capW, cp.lines * bs * 1.3 + 12], cp.text, { size: bs, align: "center", leading: bs * 1.18,
      pos: [D.W / 2, Lc.capY - (cp.lines - 1) * bs * 0.59], name: "Caption " + (k + 1), font: D.font.bold });
    var tdp = tb.property("ADBE Text Properties").property("ADBE Text Document"), dd = tdp.value;
    dd.allCaps = true; dd.applyStroke = true; dd.strokeColor = [0, 0, 0]; dd.strokeWidth = Math.max(3, Math.round(bs * 0.11));
    dd.strokeOverFill = false; tdp.setValue(dd);
    tb.property("ADBE Text Properties").property("ADBE Text More Options").property("anchorGrouping").setValue(2);   // szavanként
    if (cp.words && cp.words.length) wordAnim(tb, cp, capSt, bs);
    lays = [tb];
  } else {
    var bar = rect(main, "Caption box " + (k + 1), [Lc.capW + Lc.capPad * 2, cp.lines * Lc.capSize * 1.28 + Lc.capPad * 1.4],
      [D.W / 2, Lc.capY], [0.04, 0.045, 0.06], 72, Lc.capPad * 0.7);
    var tx = textLayer(main, [Lc.capW, cp.lines * Lc.capSize * 1.4 + 8], cp.text, { size: Lc.capSize, align: "center",
      pos: [D.W / 2, Lc.capY + Lc.capSize * 0.08], leading: Lc.capSize * 1.28, name: "Caption " + (k + 1), font: D.font.semibold });
    if (capSt !== "box" && cp.words && cp.words.length) wordAnim(tx, cp, capSt, Lc.capSize);
    lays = [bar, tx];
  }
  for (var q = 0; q < lays.length; q++) { lays[q].inPoint = cp.t0; lays[q].outPoint = cp.t1; }
}
// hang: saját hang teljes hangerőn, a zene alá keverve, a végén elhalkul (az átlátszó változatban nincs hang)
if (D.audio && D.audio.voice && !D.alpha) { var v = main.layers.add(app.project.importFile(new ImportOptions(new File(D.audio.voice)))); v.name = "Voice-over"; }
if (D.audio && D.audio.music && !D.alpha) {
  var mu = main.layers.add(app.project.importFile(new ImportOptions(new File(D.audio.music)))); mu.name = "Music";
  var lv = mu.property("ADBE Audio Group").property("ADBE Audio Levels"), g = D.audio.musicDb;
  lv.setValueAtTime(0, [g, g]); lv.setValueAtTime(Math.max(0, D.total - 2.5), [g, g]); lv.setValueAtTime(D.total, [-48, -48]);
}
// a felvétel saját hangja a zenéhez képest (music level: front = halkabb, only = néma; alapból 0 dB)
var baseDb = (D.audio && typeof D.audio.baseDb === "number") ? D.audio.baseDb : 0;
for (var bk = 0; baseDb !== 0 && !D.alpha && bk < baseLayers.length; bk++) {
  try { baseLayers[bk].property("ADBE Audio Group").property("ADBE Audio Levels").setValue([baseDb, baseDb]); }
  catch (e) { writeLn("footage sound level: " + e); }
}
// bevágott felvételek hangja: a helyén szól; „replace” — közben a felvétel saját hangja elhalkul (mint egy vágásnál)
for (var ci = 0; D.audio && D.audio.clips && !D.alpha && ci < D.audio.clips.length; ci++) {
  var cs = D.audio.clips[ci];
  var cl = main.layers.add(app.project.importFile(new ImportOptions(new File(cs.file)))); cl.name = "Cut-in sound " + (ci + 1);
  cl.startTime = cs.t0;
  if (cs.mode === "replace") for (var bj = 0; bj < baseLayers.length; bj++) {
    try {
      var bl = baseLayers[bj].property("ADBE Audio Group").property("ADBE Audio Levels");
      bl.setValueAtTime(Math.max(0, cs.t0 - 0.12), [baseDb, baseDb]); bl.setValueAtTime(cs.t0, [-48, -48]);
      bl.setValueAtTime(cs.t0 + cs.dur - 0.05, [-48, -48]); bl.setValueAtTime(cs.t0 + cs.dur + 0.15, [baseDb, baseDb]);
    } catch (e) { writeLn("cut-in sound: " + e); }
  }
}
main.openInViewer && main.openInViewer();
writeLn(JSON.stringify({ comps: app.project.numItems, scenes: (D.scenes || []).length, layers: (D.layers || []).length,
  captions: (D.captions || []).length, total: D.total }));
