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

## 1.2 One encoding profile (generation loss)
- `app/tools/encoding.py`: intermediate x264 CRF 17 (tool ops: resize, merge, trim, crop, rotate, watermark,
  gif-to-video, footage set upright, cut-ins), final x264 CRF 19, preset medium, AAC 192k, +faststart. Env:
  `VIDEO_CRF_INTERMEDIATE`, `VIDEO_CRF_FINAL`, `VIDEO_X264_PRESET`, `VIDEO_AUDIO_BITRATE`, `VIDEO_FINAL_ENCODER`.
- Final render: the engine's own H.264 only does a fixed bitrate (8 Mbps; its "quality" mode is HEVC/AV1 only), so
  the engine now writes a ProRes LT master (~100 Mbps, temp, deleted) and x264 CRF 19 makes the MP4 (tagged BT.709).
  `VIDEO_FINAL_ENCODER=engine` restores the old path; draft renders stay on the engine's fast H.264.
- merge-files was already one pass (scale + concat in one filtergraph); it stays one pass.
- Omlett chain, measured (synthetic 1080×1920 30 fps testsrc2 + blur + temporal grain, ~12.6 Mbps x264 source,
  4 s; resize-video → merge-files → video overlay; SSIM/PSNR vs the source over 114 frames, ffmpeg ssim/psnr):

  | stage | before (main a9abf31) | after |
  |---|---|---|
  | resize-video | 6.58 Mbps · SSIM 0.98638 · PSNR 46.56 dB | 13.09 Mbps · SSIM 0.98979 · PSNR 48.56 dB |
  | merge-files | 6.54 Mbps · SSIM 0.98451 · PSNR 45.44 dB | 12.41 Mbps · SSIM 0.98723 · PSNR 47.21 dB |
  | final render | 8.36 Mbps · SSIM 0.97666 · PSNR 35.32 dB (Y: 0.98020 / 37.88) | 5.52 Mbps · SSIM 0.97781 · PSNR 35.29 dB (Y: 0.98104 / 37.73) |

  The final PSNR has a ~35 dB ceiling that no bitrate lifts: the engine resamples the footage (4:2:0 → RGB →
  4:2:0, different chroma interpolation at sharp colour edges) — even its ProRes HQ output or its H.264 at 20 Mbps
  stay at 34.4 dB on this content. The final step's gain is SSIM at a smaller file; removing the ceiling needs the
  graphics composited onto the original footage in ffmpeg (alpha render + overlay), a renderer change.
  Grainier source (testsrc2 + strong noise): final 8.31 → 11.34 Mbps, SSIM 0.97284 → 0.97486.

## 3.3 HDR input (HLG / PQ)
- `color_transfer` arib-std-b67 (HLG, iPhone) or smpte2084 (PQ) → zscale to linear (npl 100) → BT.709 primaries →
  `tonemap` → BT.709 transfer, 8-bit 4:2:0, in the shared normalisation (`videonorm.head_filters`): merge-files,
  resize-video, the overlay's base footage (set upright / mapped once before the engine) and cut-in clips.
- Algorithm `VIDEO_TONEMAP` (default mobius; hable, reinhard, clip… allowed), `VIDEO_HDR_TONEMAP=0` turns it off.
  Measured on a synthetic 10-bit HEVC HLG clip (SDR testsrc2 → BT.2020/HLG, reference white 75%), signalstats vs the
  SDR original (YAVG 125 / SATAVG 112): untouched (main) 126 / 44 — saturation 39%, washed out, still tagged HLG;
  mobius 121.5 / 95.3 (97% / 85%); hable 101.3 / 77.8 (81% / 69%, darker) — hence mobius as the default.
- zimg/zscale is in the image's ffmpeg 4.4.2 (checked); the test skips where it isn't.
