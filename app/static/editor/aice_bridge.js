// aice_bridge.js — ragasztó a VÁLTOZATLAN Craft web-buildek (WordCraft, PhotoCraft, VectorCraft;
// upstream release-ek) és a conversioneditor között. Az /editor/<app>/ index.html-be a szerver
// szúrja be, a WASM elé. Mindhárom ugyanígy épül: <app>_canvas + <app>_loading, drop = megnyitás,
// Mentés/Export = letöltés-anchor.
//
// URL-paraméterek: ?src=<letöltési URL>&name=<fájlnév>&save=<POST URL>
//   src  — betöltéskor letölti a fájlt és „ráejti" a vászonra (a WordCraft drop-útja nyitja meg)
//   save — a WordCraft minden letöltését (Mentés, Exportálás) elfogja, és POST-tal a szerverre
//          küldi; hiba esetén marad a sima böngészős letöltés
// A szülő ablak (dashboard) postMessage-et kap: opened | open_failed | saved | save_failed | touched
(() => {
  const q = new URLSearchParams(location.search);
  const src = q.get("src"), name = q.get("name") || "document.docx", save = q.get("save");
  const notify = (msg) => {
    if (parent !== window) parent.postMessage({ source: "wordcraft", ...msg }, location.origin);
    console.log("[aice_bridge]", JSON.stringify(msg));
  };

  let touched = false;

  if (save) {
    const origClick = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function () {
      if (!(this.download && this.href.startsWith("blob:"))) return origClick.call(this);
      const anchor = this, fname = this.download;
      fetch(this.href)
        .then((r) => r.blob())
        .then((blob) => fetch(save + (save.includes("?") ? "&" : "?") + "name=" + encodeURIComponent(fname),
          { method: "POST", body: blob, headers: { "Content-Type": blob.type || "application/octet-stream" } }))
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status))))
        .then((j) => { touched = false; notify({ type: "saved", name: fname, ...j }); })
        .catch((e) => { notify({ type: "save_failed", name: fname, error: String(e) }); origClick.call(anchor); });
    };
  }

  // A dashboard ebből tudja, hogy elhagyás előtt rákérdezzen-e (a WordCraft saját
  // „módosítva" jelzője a WASM-ban van, kívülről nem olvasható). Sikeres mentés nullázza.
  const touch = (e) => {
    if (touched || !e.isTrusted) return;
    touched = true;
    notify({ type: "touched" });
  };
  addEventListener("keydown", touch, true);
  addEventListener("pointerdown", touch, true);

  if (src) {
    const open = async () => {
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
    // Az app akkor kész, amikor a betöltő feliratot (#<app>_loading) eltávolította.
    const wait = () => (document.querySelector('[id$="_loading"]')
      ? setTimeout(wait, 100)
      : open().catch((e) => notify({ type: "open_failed", name, error: String(e) })));
    addEventListener("TrunkApplicationStarted", wait);
  }
})();
