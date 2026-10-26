// Live preview, captions (lane L3, plan20261026_live_preview.md): the server's ASS drawn live by libass in WASM.
//
//   import { LiveCaptions } from '/static/live-captions.js'        (an ES module; also window.LiveCaptions)
//   const cap = new LiveCaptions(containerEl, { width: 960, height: 540,     // the canvas's pixel size (the part's
//                                               colors: 'server', onerror })  // frame = the server's pixels)
//   await cap.load(scene.captions)        // the scene's captions part (app/tools/live_captions.py), or null: none
//                                         // (→ true when drawn; false on an error, see onerror)
//   cap.setTime(t)                        // output seconds, from the player clock, every frame (cheap; returns a
//                                         // promise that resolves when that frame's captions are drawn)
//   cap.resize(w, h); await cap.destroy()
//
// A transparent canvas, absolutely positioned over the whole container (put the container exactly over the player's
// WebGL canvas: same box). JASSUB 2.5.20 (vendor/jassub/, build.sh there) renders it from a worker
// (OffscreenCanvas + WebGL2), single-threaded: no SharedArrayBuffer, no COOP/COEP headers needed. JASSUB is driven
// in canvas mode by our own clock — never by a <video> element — through its worker's _draw(time).
//
// Fidelity with the server (the segment preview and the final video burn the same ASS with ffmpeg's ass filter):
// - the same ASS text (PlayRes = the video's own size; libass scales it to this canvas, as ffmpeg does to its frames);
// - the same Inter files (the part's fonts, preloaded before the first frame: no fallback-font flash);
// - setTime snaps t to the frame grid (floor(t·fps)/fps, the frame a <video> shows at t): the ASS's times are snapped
//   to the frames, so a word changes on exactly the server's frame;
// - storage size = frame size, as ffmpeg's ass filter sets it;
// - colours: the server's ffmpeg 4.4 turns ASS colours into YUV with BT.601, the browser shows the video as BT.709 —
//   colors: 'server' (default) applies that same shift (JASSUB's BT.601 → BT.709 matrix); 'exact' draws the ASS
//   colours as written.
import JASSUB from './vendor/jassub/jassub.js'

const VENDOR = new URL('./vendor/jassub/', import.meta.url)
const EMPTY = '[Script Info]\nScriptType: v4.00+\n'   // the worker starts on this while the real ASS downloads
const abs = u => new URL(u, location.href).href

async function get (url, kind) {
  const r = await fetch(abs(url))
  if (!r.ok) {
    const e = new Error(`LiveCaptions: ${kind} ${r.status}${r.status === 404 ? ' (the captions changed: fetch the scene again)' : ''}`)
    e.status = r.status
    throw e
  }
  return kind === 'font' ? new Uint8Array(await r.arrayBuffer()) : r.text()
}

export class LiveCaptions {
  constructor (containerEl, opts = {}) {
    if (!containerEl) throw new Error('LiveCaptions: a container element is needed')
    this.container = containerEl
    this.width = Math.max(2, Math.round(opts.width || containerEl.clientWidth || 960))
    this.height = Math.max(2, Math.round(opts.height || containerEl.clientHeight || 540))
    this.fps = +opts.fps || 0                       // (the part's fps replaces it)
    this.colors = opts.colors || 'server'
    this.onerror = opts.onerror || null
    this.part = null
    this.t = 0
    this._j = null
    this._fonts = new Set()                          // font files the worker has
    this._want = null                                // the frame time to draw
    this._drawn = null                               // the frame time on the canvas
    this._repaint = false
    this._pump = null
    this._queue = Promise.resolve()
    this.stats = { loads: 0, loadMs: null, firstLoadMs: null, draws: 0, skipped: 0, drawMs: null, drawAvgMs: null,
      drawMaxMs: 0, errors: 0 }
    const c = this.canvas = document.createElement('canvas')
    c.className = 'live-captions'
    c.width = this.width
    c.height = this.height
    Object.assign(c.style, { position: 'absolute', left: '0', top: '0', width: '100%', height: '100%',
      pointerEvents: 'none', visibility: 'hidden' })
    if (getComputedStyle(containerEl).position === 'static') containerEl.style.position = 'relative'
    containerEl.appendChild(c)
  }

  get ready () { return this._queue }

  // the scene's captions part ({hash, ass_url, fonts, fps, …}) or null. Calls are queued in order; the same hash
  // again only refreshes the URLs (no reload). Resolves true when the new track is drawn at the current time, false
  // on an error (onerror gets it, e.status = the HTTP status; 404: the captions changed — fetch the scene again; the
  // previous track stays on).
  load (part) {
    const run = this._queue.then(() => this._load(part)).then(() => true, e => { this._fail(e); return false })
    this._queue = run
    return run
  }

  _start () {
    const j = new JASSUB({
      canvas: this.canvas,
      subContent: EMPTY,
      fonts: [],
      availableFonts: {},
      defaultFont: 'inter',
      queryFonts: false,                             // no local / Google font lookups: the fonts come with the part
      workerUrl: new URL('jassub-worker.js', VENDOR).href,
      wasmUrl: new URL('jassub-worker.wasm', VENDOR).href,
      modernWasmUrl: new URL('jassub-worker-modern.wasm', VENDOR).href
    })
    j._ro.disconnect()                               // our size, not the element's CSS box × devicePixelRatio
    j._videoWidth = this.width
    j._videoHeight = this.height
    this._j = j
    // storage size = frame size, as ffmpeg's ass filter sets it
    this._started = j.ready.then(() => j.renderer._resizeCanvas(this.width, this.height, this.width, this.height))
    return j
  }

  async _load (part) {
    const t0 = performance.now()
    if (!part || !part.ass_url) {
      this.part = null
      this.canvas.style.visibility = 'hidden'
      return
    }
    if (+part.fps > 0) this.fps = +part.fps
    if (this.part && this.part.hash === part.hash && this._j) {
      this.part = part
      return
    }
    const fonts = part.fonts || []
    const fresh = fonts.filter(f => !this._fonts.has(f.file))
    const j = this._j || this._start()               // the worker and the WASM start while the ASS and fonts download
    const [text, bins] = await Promise.all([get(part.ass_url, 'ASS'), Promise.all(fresh.map(f => get(f.url, 'font')))])
    await this._started
    if (this._j !== j) return                        // destroyed meanwhile
    if (bins.length) await j.renderer.addFonts(bins)
    fresh.forEach(f => this._fonts.add(f.file))
    await j.renderer.setTrack(text)
    await this._setColors()
    this.part = part
    this.stats.loads++
    this.canvas.style.visibility = ''               // (before the draw: a hidden placeholder drops the worker's frame)
    await this._request(true)
    this.stats.loadMs = +(performance.now() - t0).toFixed(1)
    if (this.stats.firstLoadMs === null) this.stats.firstLoadMs = this.stats.loadMs
  }

  async _setColors () {
    const r = this._j.renderer
    if (this.colors === 'server') {
      r._subtitleColorSpace = 'BT601'               // (setTrack resets it from the ASS header: set after each load)
      await r._setColorSpace('BT709')
    } else {
      await r._setColorSpace('BT709')               // the ASS says TV.709: identity
    }
  }

  _snap (t) {
    const f = this.fps
    return f > 0 ? Math.floor(t * f + 1e-3) / f : t
  }

  // the player clock, every frame: draws only when the frame changes (one draw in flight, the latest wins)
  setTime (t) {
    this.t = +t || 0
    if (!this._j || !this.part) return Promise.resolve()
    if (this._snap(this.t) === this._want && !this._repaint) {
      this.stats.skipped++
      return this._pump || Promise.resolve()
    }
    return this._request(false)
  }

  _request (repaint) {
    this._want = this._snap(this.t)
    this._repaint = this._repaint || repaint
    if (!this._pump) this._pump = this._run()
    return this._pump
  }

  async _run () {
    try {
      while (this._j && this.part && (this._want !== this._drawn || this._repaint)) {
        const t = this._want
        const rp = this._repaint
        this._repaint = false
        const t0 = performance.now()
        await this._j.renderer._draw(t, rp)
        const ms = performance.now() - t0
        this._drawn = t
        const s = this.stats
        s.draws++
        s.drawMs = +ms.toFixed(2)
        s.drawAvgMs = +((s.drawAvgMs === null ? ms : s.drawAvgMs * 0.95 + ms * 0.05)).toFixed(2)
        s.drawMaxMs = Math.max(s.drawMaxMs, +ms.toFixed(2))
      }
    } catch (e) {
      this._fail(e)
    } finally {
      this._pump = null
    }
  }

  async resize (width, height) {
    this.width = Math.max(2, Math.round(width))
    this.height = Math.max(2, Math.round(height))
    if (!this._j) return
    await this._queue
    if (!this._j) return
    this._j._videoWidth = this.width
    this._j._videoHeight = this.height
    await this._j.renderer._resizeCanvas(this.width, this.height, this.width, this.height)
    await this._request(true)
  }

  _fail (e) {
    this.stats.errors++
    console.warn('LiveCaptions:', e)
    if (this.onerror) this.onerror(e)
  }

  async destroy () {
    const j = this._j
    this._j = null
    this.part = null
    if (j) await j.destroy()
    else this.canvas.remove()
  }
}

if (typeof window !== 'undefined') window.LiveCaptions = LiveCaptions
export default LiveCaptions
