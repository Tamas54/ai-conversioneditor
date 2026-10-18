// aice_bridge.js — ragasztó a VÁLTOZATLAN Craft web-buildek (WordCraft, PhotoCraft, VectorCraft, Deck/Pdf/Design/
// GridCraft, EffectCraft, FilmCraft; upstream release-ek) és a conversioneditor között. Az /editor/<app>/
// index.html-be a szerver szúrja be, a WASM elé. A trunk-os appok: <app>_canvas + <app>_loading, drop = megnyitás,
// Mentés/Export = letöltés-anchor. Az EffectCraft és a FilmCraft nem trunk-os: a készenlétet a window.<app>Load.readyMs
// jelzi; az EffectCraft saját JS-API-t ad (effectcraft.addFile / execute / files / readFile).
//
// URL-paraméterek: ?src=<letöltési URL>&name=<fájlnév>&save=<POST URL>
//   src  — betöltéskor letölti a fájlt és megnyitja (drop, EffectCraftnál addFile — előtte a projekt médiafájljai)
//   save — a szerkesztő minden letöltését (Mentés, Exportálás, Render) elfogja, és POST-tal a szerverre
//          küldi; hiba esetén marad a sima böngészős letöltés
// A szülő ablak (dashboard) postMessage-et kap: opened | open_failed | saved | save_failed | touched | activity |
//   snapshot | snapshot_failed. A „snapshot” parancs (figyelő mód): ugyanaz a Ctrl+S, de a bájtok NEM új változatként
//   mennek fel, hanem ideiglenes másolatként (/api/editor/snapshot) — a „touched” állapot marad.
(() => {
  const q = new URLSearchParams(location.search);
  const src = q.get("src"), name = q.get("name") || "document.docx", save = q.get("save");
  const app = (location.pathname.match(/\/editor\/([a-z]+)\//) || [])[1] || "";
  const source = save ? new URLSearchParams(save.split("?")[1] || "").get("source") : null;
  const notify = (msg) => {
    if (parent !== window) parent.postMessage({ source: "wordcraft", ...msg }, location.origin);
    console.log("[aice_bridge]", JSON.stringify(msg));
  };
  const MEDIA = /\.(png|jpe?g|webp|gif|tiff?|bmp|exr|psd|svg|mp4|mov|webm|m4v|mkv|mp3|wav|m4a|aac|ogg|oga|flac|aiff?|opus)$/i;
  const known = new Set();      // a projekt ismert médiafájl-nevei (EffectCraft) — mentés után csak az újak mennek fel

  let touched = false, savingAt = 0;   // savingAt: mikor indult az utolsó mentés-letöltés
  let snapNext = false, lastActivity = 0;   // snapNext: a következő mentés-letöltés pillanatkép (nem változat)

  // EffectCraft: a böngészőben a projekthez adott képek/hangok/filmek a mentett projekt mellé a szerverre
  const uploadNewMedia = async (projectId) => {
    if (app !== "effectcraft" || !window.effectcraft || !projectId) return;
    let list = [];
    try { list = await effectcraft.files(); } catch { return; }
    for (const f of list || []) {
      const base = String(f.path || "").split("/").pop();
      if (!base || known.has(base) || !MEDIA.test(base) || (f.size || 0) > 200 * 1024 * 1024) continue;
      try {
        const bytes = await effectcraft.readFile(f.path);
        const r = await fetch(`/api/video/asset?project=${encodeURIComponent(projectId)}&name=${encodeURIComponent(base)}`,
          { method: "POST", body: bytes, headers: { "Content-Type": "application/octet-stream" } });
        if (r.ok) known.add(base);
      } catch (e) { console.warn("[aice_bridge] media upload", base, e); }
    }
  };

  if (save) {
    const origClick = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function () {
      if (!(this.download && this.href.startsWith("blob:"))) return origClick.call(this);
      const anchor = this, fname = this.download;
      if (snapNext) {                       // figyelő mód: ideiglenes másolat, nem változat; letöltés sincs
        snapNext = false;
        if (!/\.docx$/i.test(fname)) { notify({ type: "snapshot_failed", error: "not a Word document" }); return; }
        fetch(this.href).then((r) => r.blob())
          .then((blob) => fetch("/api/editor/snapshot", { method: "POST", body: blob, headers: { "Content-Type": "application/octet-stream" } }))
          .then((r) => (r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status))))
          .then((j) => notify({ type: "snapshot", ...j }))
          .catch((e) => notify({ type: "snapshot_failed", error: String(e) }));
        return;
      }
      savingAt = Date.now();
      fetch(this.href)
        .then((r) => r.blob())
        .then((blob) => fetch(save + (save.includes("?") ? "&" : "?") + "name=" + encodeURIComponent(fname),
          { method: "POST", body: blob, headers: { "Content-Type": blob.type || "application/octet-stream" } }))
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status))))
        .then(async (j) => {
          if (/\.ecproj$/i.test(fname)) await uploadNewMedia(j.file_id);
          touched = false; notify({ type: "saved", name: fname, ...j });
        })
        .catch((e) => { notify({ type: "save_failed", name: fname, error: String(e) }); origClick.call(anchor); });
    };
  }

  // A dashboard ebből tudja, hogy elhagyás előtt rákérdezzen-e (a szerkesztő saját
  // „módosítva" jelzője a WASM-ban van, kívülről nem olvasható). Sikeres mentés nullázza.
  // Csak nézelődés (nyíl, lapozás, Esc, Tab, F-gombok, másolás/keresés/nagyítás/nyomtatás, magában álló módosító):
  // nem változtatás → nem jelez „touched”-ot (különben a puszta görgetés után is „mentetlen változás” kérdés jön)
  const LOOK = /^(Arrow\w+|Page(Up|Down)|Home|End|Escape|Tab|F\d+|Shift|Control|Alt|Meta|CapsLock)$/;
  const lookOnly = (e) => e.type === "keydown" && (LOOK.test(e.key) || ((e.ctrlKey || e.metaKey) && /^[cfap+=\-0]$/i.test(e.key)));
  const touch = (e) => {
    if (!e.isTrusted) return;
    const now = Date.now();
    if (now - lastActivity > 3000) { lastActivity = now; notify({ type: "activity" }); }   // a figyelő a szünetet várja
    if (touched || lookOnly(e)) return;
    touched = true;
    notify({ type: "touched" });
  };
  addEventListener("keydown", touch, true);
  addEventListener("pointerdown", touch, true);

  // A szülő kérésére mentés (a „Kérj változtatást” előtt): a szerkesztő saját Ctrl+S-e — a mentés-letöltést
  // a fenti anchor-elfogás küldi a szerverre, és „saved” üzenet jön vissza. (Szintetikus esemény: a touch nem jelzi.)
  addEventListener("message", (e) => {
    if (e.source !== parent || e.origin !== location.origin || e.data?.target !== "aice_bridge") return;
    if (e.data.cmd !== "save" && e.data.cmd !== "snapshot") return;
    if (e.data.cmd === "snapshot") { if (app !== "wordcraft") return; snapNext = true; setTimeout(() => { snapNext = false; }, 15000); }
    if (app === "effectcraft" && window.effectcraft) {     // saját parancs: File ▸ Save (a böngészőben le is tölti)
      effectcraft.execute("file.save", {}).catch((err) => notify({ type: "save_failed", name, error: String(err) }));
      return;
    }
    const canvas = document.querySelector("canvas");
    const press = (key, code, keyCode, ctrlKey) => {
      for (const type of ["keydown", "keyup"])
        (canvas || document).dispatchEvent(new KeyboardEvent(type, { key, code, keyCode, which: keyCode, ctrlKey,
          bubbles: true, cancelable: true }));
    };
    canvas?.focus();
    const asked = Date.now();
    press("s", "KeyS", 83, true);
    // A VectorCraft Ctrl+S-re „Save As” ablakot nyit (OK = Enter); máshol az Enter szöveget írna — csak ott
    if (/\/editor\/vectorcraft\//.test(location.pathname))
      setTimeout(() => { if (savingAt < asked) { canvas?.focus(); press("Enter", "Enter", 13, false); } }, 1500);
  });

  if (src) {
    const drop = async () => {
      const r = await fetch(src);
      if (!r.ok) throw new Error("HTTP " + r.status);
      const blob = await r.blob();
      const dt = new DataTransfer();
      dt.items.add(new File([blob], name, { type: blob.type }));
      const canvas = document.querySelector("canvas");
      for (const t of ["dragenter", "dragover", "drop"])
        canvas.dispatchEvent(new DragEvent(t, { dataTransfer: dt, bubbles: true, cancelable: true }));
      notify({ type: "opened", name, bytes: blob.size });
    };
    // EffectCraft: előbb a projekt médiafájljai (a projekt relatív névvel hivatkozik rájuk), aztán maga a projekt
    const addFiles = async () => {
      if (/\.ecproj$/i.test(name) && source) {
        const a = await fetch(`/api/video/assets?file_id=${encodeURIComponent(source)}`).then((r) => (r.ok ? r.json() : { assets: [] }));
        for (const x of a.assets || []) { await effectcraft.addFile(x.url, x.name); known.add(x.name); }
      }
      const r = await fetch(src);
      if (!r.ok) throw new Error("HTTP " + r.status);
      const blob = await r.blob();
      await effectcraft.addFile(new File([blob], name), name);
      notify({ type: "opened", name, bytes: blob.size });
    };
    const fail = (e) => notify({ type: "open_failed", name, error: String(e) });
    if (app === "effectcraft" || app === "filmcraft") {
      const load = () => window[app + "Load"] || {};
      const wait = () => (load().readyMs ? (app === "effectcraft" ? addFiles() : drop()).catch(fail)
        : load().error || load().fatal ? fail(load().error || load().fatal) : setTimeout(wait, 150));
      wait();
    } else {
      // Az app akkor kész, amikor a betöltő feliratot (#<app>_loading) eltávolította.
      const wait = () => (document.querySelector('[id$="_loading"]') ? setTimeout(wait, 100) : drop().catch(fail));
      addEventListener("TrunkApplicationStarted", wait);
    }
  }
})();
