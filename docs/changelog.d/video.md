# Lane "video" (OPERATION OMLETT)

## 1.3 — the video check sees text collisions
- A graphic's `text` without `w` no longer wraps at a hidden 0.6 of the frame: it stays on one line while it fits in
  0.92 of the frame width (measured with Inter, the renderer's font — ő/ű included; short texts keep their 0.6 box, so
  older videos look the same; left/right-aligned text keeps its old anchor edge).
- The builder reports every graphic text's drawn (ink) box from the engine (`sourceRectAtTime`); the check turns them into
  findings: two texts overlapping (same layer or two layers on screen together), a text running out of its enclosing
  `rect` or off the frame, an unexpected line break (no `w`). Each finding names the layer/scene, the element and a
  suggested `w` / `size` (or `y`). Status becomes `needs_check`.
- `render=false` (overlay / make / edit) still returns this layout check.
- Measured on the reconstructed 10-19 layers (`rendkivuli`, `tudta`, `hir3`, main's 0.6 box): main 0 findings / `ok`,
  now 3 named findings / `needs_check`. Without `w` the new default keeps "RENDKÍVÜLI KIADÁS" (66 px, ~663 px wide) and
  the Tudta line on one line; only the over-long ticker text is still reported (with a size that fits).
