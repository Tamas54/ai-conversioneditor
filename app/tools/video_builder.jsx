// Videó-építő az EffectCraft After Effects-szerű szkript-objektummodelljére (effectcraft-cli script).
// A Python (app/tools/video.py) elé teszi:  var D = {…};  — minden méret, idő, szöveg és fájlnév onnan jön,
// itt csak a felépítés van. Szerkezet (a szerkesztőben is így látszik):
//   „Video” fő kompozíció: jelenetenként egy beágyazott kompozíció (Title · Scene 1…n · End), áttűnéssel
//   egymásba csúsztatva; fölöttük a feliratok (Caption …, sávval); alul a zene / a saját hang.
//   Jelenet: kép Ken Burns-mozgással (vagy színátmenet, ha nincs kép) · sötét átmenet alul · akcentus-csík ·
//   cím (beúszik) · opcionális nagy szám (stat).

function tp(layer) { return layer.property("ADBE Transform Group"); }
function ease(prop) {   // lágy indulás/érkezés minden kulcskockán (ha a motor nem tudja, marad lineáris)
  try {
    for (var k = 1; k <= prop.numKeys; k++)
      prop.setTemporalEaseAtKey(k, [new KeyframeEase(0, 60)], [new KeyframeEase(0, 60)]);
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
function rect(comp, name, size, pos, color, opacity, round) {
  var s = comp.layers.addShape(); s.name = name;
  var g = s.property("ADBE Root Vectors Group").addProperty("ADBE Vector Group");
  var r = g.property("ADBE Vectors Group").addProperty("ADBE Vector Shape - Rect");
  r.property("ADBE Vector Rect Size").setValue(size);
  if (round) r.property("ADBE Vector Rect Roundness").setValue(round);
  var f = g.property("ADBE Vectors Group").addProperty("ADBE Vector Graphic - Fill");
  f.property("ADBE Vector Fill Color").setValue([color[0], color[1], color[2], 1]);
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
function background(comp, sc, dur) {
  if (sc.image) {
    var f = FOOT[sc.image] || (FOOT[sc.image] = app.project.importFile(new ImportOptions(new File(sc.image))));
    var L = comp.layers.add(f); L.name = "Picture";
    var s = Math.max(D.W / f.width, D.H / f.height) * 100, z = s * 1.12;
    var S = tp(L).property("ADBE Scale"), P = tp(L).property("ADBE Position");
    var m = sc.motion || "in", dx = D.W * 0.035;
    if (m === "kb-in" || m === "kb-out") {
      // Ken Burns: közelítés ÉS úsztatás egyszerre, a kép érdekes pontja (sc.focus, 0–1) a keret közepe felé;
      // a pozíció úgy korlátozva, hogy a kép végig kitöltse a keretet (lineáris skála + lineáris út → közben is)
      var Z = s * 1.28, fo = sc.focus || [0.5, 0.45], hw = f.width * Z / 200, hh = f.height * Z / 200;
      var px = D.W / 2 - (fo[0] * f.width - f.width / 2) * Z / 100, py = D.H / 2 - (fo[1] * f.height - f.height / 2) * Z / 100;
      px = Math.min(hw, Math.max(D.W - hw, px)); py = Math.min(hh, Math.max(D.H - hh, py));
      var a = [[s, s], [D.W / 2, D.H / 2]], b = [[Z, Z], [px, py]];
      if (m === "kb-out") { var tmp = a; a = b; b = tmp; }
      S.setValueAtTime(0, a[0]); S.setValueAtTime(dur, b[0]); P.setValueAtTime(0, a[1]); P.setValueAtTime(dur, b[1]);
    } else if (m === "out") { S.setValueAtTime(0, [z, z]); S.setValueAtTime(dur, [s, s]); }
    else if (m === "left" || m === "right") {
      var k = s * 1.09, sign = m === "left" ? 1 : -1;
      S.setValue([k, k]);
      P.setValueAtTime(0, [D.W / 2 + sign * dx, D.H / 2]); P.setValueAtTime(dur, [D.W / 2 - sign * dx, D.H / 2]);
    } else { S.setValueAtTime(0, [s, s]); S.setValueAtTime(dur, [z, z]); }
    if (sc.blur) {
      var b = L.property("ADBE Effect Parade").addProperty("Gaussian Blur");
      b.property("Blurriness").setValue(sc.blur); b.property("Repeat Edge Pixels").setValue(1);
    }
  } else {   // nincs kép: két akcentus-tónusú lágy színátmenet
    var g = comp.layers.addSolid(D.dark, "Background", D.W, D.H, 1, dur);
    var fx = g.property("ADBE Effect Parade").addProperty("Gradient Ramp");
    fx.property("Start of Ramp").setValue([0, 0]); fx.property("Start Color").setValue([D.accent[0] * 0.55, D.accent[1] * 0.55, D.accent[2] * 0.55]);
    fx.property("End of Ramp").setValue([D.W, D.H]); fx.property("End Color").setValue(D.dark);
  }
}
var SHADES = {};
function shade(comp, dur, kind) {   // alul sötétedő átmenet: előre megrajzolt PNG (olcsó); „even” = egyenletes sötétítés
  if (kind === "even" || !D.shade || !D.shade[kind]) {
    var s = comp.layers.addSolid([0, 0, 0], "Shade", D.W, D.H, 1, dur);
    tp(s).property("ADBE Opacity").setValue(kind === "even" ? 70 : 60);
    return s;
  }
  SHADES[kind] = SHADES[kind] || app.project.importFile(new ImportOptions(new File(D.shade[kind])));
  var L = comp.layers.add(SHADES[kind]); L.name = "Shade";
  return L;
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
  shade(c, sc.dur, "title");
  var L = D.L;
  if (sc.kicker) fadeIn(textLayer(c, [L.titleW, L.kickerSize * 1.6], sc.kicker.toUpperCase(),
    { size: L.kickerSize, color: D.accent, align: L.titleAlign, pos: [L.titleX, L.kickerY], tracking: 80, name: "Kicker", font: D.font.semibold }), 0.2, 0.5, 0);
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
  } else if (g === "pop") {
    S.setValueAtTime(el.t0, [0, 0]); S.setValueAtTime(el.t0 + el.dur, [100, 100]); ease(S);
  } else if (el.t0 !== undefined) {
    var o = T.property("ADBE Opacity"), full = el.opacity || 100;
    o.setValueAtTime(el.t0, 0); o.setValueAtTime(el.t0 + el.dur, full);
  }
}
function chartLayers(c, ch) {   // a Python (chart_elements) számolta geometria: minden oszlop / pont / felirat külön réteg
  var F = { bold: D.font.bold, semibold: D.font.semibold, regular: D.font.regular };
  for (var k = 0; k < ch.elements.length; k++) {
    var el = ch.elements[k];
    if (el.k === "rect") {
      var r = rect(c, el.name, el.size, el.pos, el.color === "accent" ? D.accent : el.color, el.opacity, el.round);
      if (el.rot) tp(r).property("ADBE Rotate Z").setValue(el.rot);
      if (el.grow) grower(r, el);
    } else {
      fadeIn(textLayer(c, el.box, el.str, { size: el.size, align: el.align, pos: el.pos, name: el.name,
        font: F[el.font] || D.font.semibold, color: el.color }), el.t0 || 0, 0.4, 0);
    }
  }
}
function contentScene(sc, i) {
  var c = app.project.items.addComp("Scene " + i + " – " + sc.headline.substring(0, 32), D.W, D.H, 1, sc.dur, D.fps);
  background(c, sc, sc.dur);
  var L = D.L;
  if (sc.chart) {        // mozgó grafikon: sötétített kép, fent a cím, alatta a grafikon
    shade(c, sc.dur, "even");
    var cs = Math.round(Math.min(D.W, D.H) * 0.052);
    fadeIn(textLayer(c, [D.W * 0.84, cs * 2.6], sc.chart.title, { size: cs, align: "center", pos: [D.W / 2, D.H * 0.1],
      leading: cs * 1.1, name: "Chart title" }), 0.1, 0.5, D.H * 0.015);
    chartLayers(c, sc.chart);
    return c;
  }
  if (sc.stat) {
    shade(c, sc.dur, "even");
    var ss = sc.stat.size || L.statSize;
    fadeIn(textLayer(c, [D.W * 0.94, ss * 1.3], sc.stat.value, { size: ss, color: D.accent, align: "center",
      pos: [D.W / 2, L.statY + (L.statSize - ss) * 0.4], name: "Number" }), 0.2, 0.6, D.H * 0.03);
    fadeIn(textLayer(c, [D.W * 0.8, L.statLabelSize * 3.2], sc.stat.label, { size: L.statLabelSize, align: "center",
      pos: [D.W / 2, L.statLabelY], name: "Number label", font: D.font.semibold }), 0.5, 0.6, D.H * 0.02);
  } else {
    shade(c, sc.dur, "scene");
    accentBar(c, L.headX - L.headW / 2, L.headBarY, 0.1, false);
    fadeIn(textLayer(c, [L.headW, L.headH], sc.headline, { size: sc.size, align: "left", pos: [L.headX, L.headY],
      leading: sc.size * 1.12, name: "Headline" }), 0.25, 0.6, D.H * 0.02);
  }
  return c;
}
function endScene(sc) {
  var c = app.project.items.addComp("End", D.W, D.H, 1, sc.dur, D.fps);
  background(c, sc, sc.dur);              // a kép itt előre elmosott (a Python készíti), nincs képkockánkénti blur
  if (sc.image) shade(c, sc.dur, "even");
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

var main = app.project.items.addComp("Video", D.W, D.H, 1, D.total, D.fps);
main.bgColor = D.dark;
var order = [titleScene(D.title)], starts = [0], t = D.title.dur - D.xfade;
for (var i = 0; i < D.scenes.length; i++) { order.push(contentScene(D.scenes[i], i + 1)); starts.push(t); t += D.scenes[i].dur - D.xfade; }
order.push(endScene(D.end)); starts.push(t);
for (var j = 0; j < order.length; j++) {    // alulról fölfelé: a későbbi jelenet a korábbi FÖLÉ kerül, és beúszik
  var L = main.layers.add(order[j]);
  L.startTime = starts[j];
  if (j > 0) { var o = tp(L).property("ADBE Opacity"); o.setValueAtTime(starts[j], 0); o.setValueAtTime(starts[j] + D.xfade, 100); }
}
// feliratok: sáv + szöveg, a jelenetek fölött (a szerkesztőben egyenként átírhatók)
for (var k = 0; k < D.captions.length; k++) {
  var cp = D.captions[k], Lc = D.L;
  var bar = rect(main, "Caption box " + (k + 1), [Lc.capW + Lc.capPad * 2, cp.lines * Lc.capSize * 1.28 + Lc.capPad * 1.4],
    [D.W / 2, Lc.capY], [0.04, 0.045, 0.06], 72, Lc.capPad * 0.7);
  var tx = textLayer(main, [Lc.capW, cp.lines * Lc.capSize * 1.4 + 8], cp.text, { size: Lc.capSize, align: "center",
    pos: [D.W / 2, Lc.capY + Lc.capSize * 0.08], leading: Lc.capSize * 1.28, name: "Caption " + (k + 1), font: D.font.semibold });
  var lays = [bar, tx];
  for (var q = 0; q < 2; q++) { lays[q].inPoint = cp.t0; lays[q].outPoint = cp.t1; }
}
// hang: saját hang teljes hangerőn, a zene alá keverve, a végén elhalkul
if (D.audio.voice) { var v = main.layers.add(app.project.importFile(new ImportOptions(new File(D.audio.voice)))); v.name = "Voice-over"; }
if (D.audio.music) {
  var mu = main.layers.add(app.project.importFile(new ImportOptions(new File(D.audio.music)))); mu.name = "Music";
  var lv = mu.property("ADBE Audio Group").property("ADBE Audio Levels"), g = D.audio.musicDb;
  lv.setValueAtTime(0, [g, g]); lv.setValueAtTime(Math.max(0, D.total - 2.5), [g, g]); lv.setValueAtTime(D.total, [-48, -48]);
}
main.openInViewer && main.openInViewer();
writeLn(JSON.stringify({ comps: app.project.numItems, scenes: D.scenes.length, captions: D.captions.length, total: D.total }));
