# OPERATION OMLETT — media lane

## 1.1 merge-files keeps portrait clips portrait
- The canvas comes from the first clip's DISPLAY size (after rotation: displaymatrix side data and the legacy `rotate`
  tag are both read) — 2 portrait iPhone-style clips (coded 1920×1080, rotation 90) + 1 landscape → 1080×1920
  (it was 1920×1080 with the portrait clips in a ~608 px strip).
- New merge-files options: `canvas` = auto | vertical | landscape | square | WxH, `fit` = blur (default: a clip of
  another shape sits on its own blurred copy) | contain (black bars) | cover (fill, edges cut).
- resize-video: `aspect_mode = blur` (same helper).
- Shared modules: `app/tools/mediaprobe.py` (duration, display size, rotation, fps, codecs, audio, HDR, bitrate) and
  `app/tools/videonorm.py` (canvas size, fit chains).
