# Encode Performance & Flickering

## VideoToolbox HDR Flickering

`hevc_videotoolbox` with 10-bit HDR (`p010le`) can produce intermittent brightness flicker on macOS, especially across concat-stitched segment boundaries. Each trimmed segment starts a fresh VideoToolbox encode session, and the rate control can flash on the first GOP.

**Workarounds (try in order):**

1. **Switch to software encode** (`--cpu-only`) — uses `libx265`. 5x slower wall time but no flicker. Reliable fallback for quality-sensitive output.
2. **VideoToolbox encoder params** — pass `-encoder_params` flags to stabilize the decoder. Not currently in the script; would need a script patch to add e.g. `-encoder_params "allow_async_compose=1"` or `-encoder_params "disable_frame_threading=1"`.
3. **Single-segment test** — if the flicker only appears at stitch boundaries, it's a concat rate-control reset issue. Run on a single continuous segment (no dead time) to isolate.

## Timing Ratios (iPhone HDR 1080p, ~140s source)

These are relative ratios, not absolute numbers — actual wall time scales with source duration and resolution.

| Mode | Relative Speed | Wall Time (2-2.5min source) | Notes |
|------|---------------|----------------------------|-------|
| Hardware (`hevc_videotoolbox`) + motion | 1x (baseline) | ~29s | Full motion + HDR encode |
| Hardware + `--no-motion` | ~0.7x | ~21s | Skips frame extraction + MAD (~8s saved) |
| Software (`libx265`) + `--no-motion` | ~5x | ~2m27s | Quality reference, no flicker |
| `-c copy` concat (hypothetical) | ~0.1x | ~3-5s | No re-encode at all |

The analysis phase (audio peak detection) is negligible (~0.5-1s). Dominant cost is always the FFmpeg re-encode.

## `-c copy` Stream-Copy Concat

**Not currently implemented in the script**, but worth noting as a potential fast-path:

- Skips re-encode entirely — just demuxes, trims, and concatenates the source packets
- Wall time drops to roughly the time to copy bytes (~3-5s for a 685M file)
- Major tradeoff: **keyframe snapping**. `-c copy` can only cut at I-frames. iPhone footage typically has ~1-2s GOP intervals. The serve buffer / follow-through boundaries drift to the nearest keyframe, which can clip the first hit or leave dead time at the end.

To implement, the concat **demuxer** is used instead of the concat filter — requires writing a temporary concat file listing the raw media segments, then running `ffmpeg -f concat -i list.txt -c copy output.MP4`.

The `-tag:v hvc1` check: iPhone sources already record with `hvc1`, so stream copy preserves it. Non-iPhone sources may need explicit tag forcing.