// Stage layer (N5, app/tools/stage_layer.py): the Python computes every number (the pictures, the projected corners,
// the entrance keys, the periodic motions); this only lays them down in the builder layer's own comp c. Appended to
// video_builder.jsx by video._build; overlayLayers calls stageLayer(c, ly) for a layer with ly.stage.
// Every periodic motion is an EXPRESSION on the integer frame number (f): frame f and f + pf are bit-identical, so the
// live preview's loop probe finds the cycle (app/tools/live_layers.py) and the clip is the entrance + one period.

function stageExpr(pf, fps) {
  return "var f = Math.round(time * " + fps + "); var u = (((f % " + pf + ") + " + pf + ") % " + pf + ") / " + pf +
    "; var p = u * 2 * Math.PI; ";
}
function stageFile(file) { return FOOT[file] || (FOOT[file] = app.project.importFile(new ImportOptions(new File(file)))); }
function stageShadow(L, sh) {
  if (!sh) return;
  try {
    var ds = L.property("ADBE Effect Parade").addProperty("Drop Shadow");
    ds.property("Opacity").setValue(sh.opacity); ds.property("Distance").setValue(sh.distance);
    ds.property("Softness").setValue(sh.softness); ds.property("Direction").setValue(165);
  } catch (e) { writeLn("stage shadow: " + e); }
}
function stageMask(L, x, y, w, h, r) {        // a rounded-rectangle mask in the layer's own pixels
  try {
    var M = L.property("ADBE Mask Parade").addProperty("ADBE Mask Atom"), sh = new Shape(), k = 0.5523 * r;
    sh.vertices = [[x + r, y], [x + w - r, y], [x + w, y + r], [x + w, y + h - r], [x + w - r, y + h], [x + r, y + h], [x, y + h - r], [x, y + r]];
    sh.inTangents = [[-k, 0], [0, 0], [0, -k], [0, 0], [k, 0], [0, 0], [0, k], [0, 0]];
    sh.outTangents = [[0, 0], [k, 0], [0, 0], [0, k], [0, 0], [-k, 0], [0, 0], [0, -k]];
    sh.closed = true;
    M.property("ADBE Mask Shape").setValue(sh);
  } catch (e) { writeLn("stage mask: " + e); }
}
function stageLoop(L, src, dur) {             // a clip shorter than its span plays again from its start
  try {
    if (src.duration && src.duration < dur - 0.05) {
      L.timeRemapEnabled = true;
      L.property("ADBE Time Remapping").expression = 'loopOut("cycle")';
      L.outPoint = dur;
    }
  } catch (e) { writeLn("stage loop: " + e); }
}
function stageMotion(L, s, pre) {             // a sprite's periodic motions (stage_layer.background_sprites)
  var T = tp(L), P = T.property("ADBE Position"), R = T.property("ADBE Rotate Z"), S = T.property("ADBE Scale"),
    O = T.property("ADBE Opacity");
  var px = [], py = [];
  if (s.orbit) {
    px.push(s.orbit[0] + " * Math.sin(" + s.orbit[2] + " * p + " + s.orbit[3] + ")");
    py.push(s.orbit[1] + " * Math.cos(" + s.orbit[2] + " * p + " + s.orbit[3] + ")");
  }
  if (s.slide) { px.push(s.slide[0] + " * u"); py.push(s.slide[1] + " * u"); }
  if (px.length) P.expression = pre + "[value[0] + " + px.join(" + ") + ", value[1] + " + py.join(" + ") + "]";
  var rot = [];
  if (s.spin) rot.push(s.spin + " * u");
  if (s.rot) rot.push(s.rot[0] + " * Math.sin(" + s.rot[1] + " * p + " + s.rot[2] + ")");
  if (rot.length) R.expression = pre + "value + " + rot.join(" + ");
  if (s.breathe) S.expression = pre + "var k = 1 + " + s.breathe[0] + " * Math.sin(" + s.breathe[1] + " * p + " + s.breathe[2] +
    "); [value[0] * k, value[1] * k]";
  if (s.twinkle) O.expression = pre + s.twinkle[0] + " + " + (s.twinkle[1] - s.twinkle[0]) + " * (0.5 + 0.5 * Math.sin(" +
    s.twinkle[2] + " * p + " + s.twinkle[3] + "))";
}
function stageBackground(c, ly) {
  var st = ly.stage, pre = stageExpr(st.pf, st.fps), video = !!(st.video && st.video.file);
  for (var i = 0; i < st.sprites.length; i++) {
    var s = st.sprites[i];
    if (video && s.over && !st._vid) {        // the clip over the base, under the vignette
      st._vid = true;
      var vf = stageFile(st.video.file), V = c.layers.add(vf); V.name = "Background clip";
      try { V.audioEnabled = false; } catch (e) {}
      var k = Math.max(c.width / st.video.size[0], c.height / st.video.size[1]) * 100;
      tp(V).property("ADBE Scale").setValue([k, k]); tp(V).property("ADBE Position").setValue([c.width / 2, c.height / 2]);
      stageLoop(V, vf, ly.dur);
      var dm = c.layers.addSolid(st.video.dim_color, "Dim", c.width, c.height, 1);
      tp(dm).property("ADBE Opacity").setValue(Math.round(st.video.dim * 100));
    }
    var L = c.layers.add(stageFile(s.file)); L.name = "Sprite " + (i + 1);
    tp(L).property("ADBE Scale").setValue([s.scale, s.scale]);
    tp(L).property("ADBE Position").setValue(s.pos);
    if (s.opacity !== undefined && s.opacity !== 100) tp(L).property("ADBE Opacity").setValue(s.opacity);
    stageMotion(L, s, pre);
  }
}
function stagePanel(c, ly) {
  var st = ly.stage, cw = st.canvas[0], ch = st.canvas[1], src;
  if (st.video) {                             // a playing clip: rounded by a mask, under the frame picture, in a padded comp
    var cc = app.project.items.addComp(ly.name + " card", cw, ch, 1, ly.dur, D.fps);
    var vf = stageFile(st.video.file), V = cc.layers.add(vf); V.name = "Clip";
    try { V.audioEnabled = false; } catch (e) {}
    var vw = st.video.size[0], vh = st.video.size[1], k = Math.max(st.card[0] / vw, st.card[1] / vh);
    tp(V).property("ADBE Scale").setValue([k * 100, k * 100]); tp(V).property("ADBE Position").setValue([cw / 2, ch / 2]);
    var mw = st.card[0] / k, mh = st.card[1] / k, r = Math.max(6, 0.028 * Math.min(st.card[0], st.card[1])) / k;
    stageMask(V, (vw - mw) / 2, (vh - mh) / 2, mw, mh, r);
    stageLoop(V, vf, ly.dur);
    var Fr = cc.layers.add(stageFile(st.file)); Fr.name = "Frame";
    tp(Fr).property("ADBE Position").setValue([cw / 2, ch / 2]);
    src = cc;
  } else src = stageFile(st.file);
  var L = c.layers.add(src); L.name = "Panel";
  var T = tp(L), O = T.property("ADBE Opacity");
  T.property("ADBE Position").setValue([cw / 2, ch / 2]);
  var cp = L.property("ADBE Effect Parade").addProperty("Corner Pin");
  var pn = ["Upper Left", "Upper Right", "Lower Left", "Lower Right"];
  if (st.keys && st.keys.length) {            // the entrance: dense keys (every 50 ms), straight between them
    for (var i = 0; i < st.keys.length; i++) {
      var kf = st.keys[i];
      for (var j = 0; j < 4; j++) cp.property(pn[j]).setValueAtTime(kf[0], kf[1 + j]);
      O.setValueAtTime(kf[0], kf[5]);
    }
  } else for (var q = 0; q < 4; q++) cp.property(pn[q]).setValue(st.pins[q]);
  stageShadow(L, st.shadow);
  var fl = st.float;
  if (fl) T.property("ADBE Position").expression = stageExpr(fl.pf, fl.fps) + "[value[0] + " + fl.ax + " * Math.sin(p + " +
    fl.ph + "), value[1] + " + fl.ay + " * Math.sin(p + " + (fl.ph + 1.7).toFixed(4) + ")]";
}
function stagePill(c, ly) {
  var st = ly.stage, w = ly.w, h = ly.h;
  var t = c.layers.addBoxText([w, st.size * 1.8], st.text);
  var p = t.property("ADBE Text Properties").property("ADBE Text Document"), d = p.value;
  d.font = D.font.bold; d.fontSize = st.size; d.fillColor = st.ink;
  d.justification = ParagraphJustification.CENTER_JUSTIFY;
  p.setValue(d);
  t.name = "Label";
  var T = tp(t);
  T.property("ADBE Position").setValue([w / 2, h / 2]);
  var bw = w * 0.7, bh = st.size * 1.5;
  try {                                       // the rounded box sized to the drawn text; both scale about its centre
    var r = t.sourceRectAtTime(0, false);
    T.property("ADBE Anchor Point").setValue([r.left + r.width / 2, r.top + r.height / 2]);
    bw = r.width + 2 * st.pad[0]; bh = Math.max(st.size * 0.95, r.height) + 2 * st.pad[1];
  } catch (e) { writeLn("stage pill measure: " + e); }
  var R = rect(c, "Pill", [bw, bh], [w / 2, h / 2], st.fill, 100, Math.min(st.radius, bh / 2));
  R.moveAfter(t);
  stageShadow(R, st.shadow);
  var ks = [[0, 35], [0.16, 112], [0.27, 96], [0.36, 100]];
  var L2 = [t, R];
  for (var i = 0; i < 2; i++) {
    var S = tp(L2[i]).property("ADBE Scale"), O = tp(L2[i]).property("ADBE Opacity");
    for (var j = 0; j < ks.length; j++) S.setValueAtTime(ks[j][0], [ks[j][1], ks[j][1]]);
    O.setValueAtTime(0, 0); O.setValueAtTime(0.12, 100);
    ease(S);
  }
}
function stageGroup(c, ly) {                  // the background + the panels that stay the whole stage, in one comp
  var parts = ly.stage.parts;
  for (var i = 0; i < parts.length; i++) {
    var pt = parts[i];
    if (pt.role === "bg") { stageBackground(c, { stage: pt, dur: ly.dur, name: ly.name }); continue; }
    var cc = app.project.items.addComp(ly.name + " – panel " + (i + 1), pt.canvas[0], pt.canvas[1], 1, Math.max(0.5, pt.dur), D.fps);
    stagePanel(cc, { stage: pt, dur: pt.dur, name: ly.name + " – panel " + (i + 1) });
    var L = c.layers.add(cc); L.name = "Panel " + (i + 1); L.startTime = pt.t0;
    var P = tp(L).property("ADBE Position");
    P.setValue(pt.pos);
    if (pt.slide && (pt.slide[0] || pt.slide[1])) {   // the fly-in (as the builder's "slide", inside the group)
      P.setValueAtTime(pt.t0, [pt.pos[0] + pt.slide[0], pt.pos[1] + pt.slide[1]]); P.setValueAtTime(pt.t0 + 0.45, pt.pos);
      ease(P);
    }
  }
}
function stageLayer(c, ly) {
  var role = ly.stage.role;
  if (role === "group") stageGroup(c, ly);
  else if (role === "bg") stageBackground(c, ly);
  else if (role === "panel") stagePanel(c, ly);
  else if (role === "pill") stagePill(c, ly);
}
