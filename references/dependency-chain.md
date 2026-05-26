# Dependency Chain & Graceful Degradation

## matplotlib's Dual Role

`matplotlib` is used for two distinct things:

1. **Frame reading** (`plt.imread()` in `analyze_motion()`) — loads JPEG frames for the MAD motion computation. This is the core motion pipeline.
2. **Plotting** — the `--plot` flag (removed from skill docs but still supported by the script).

## Degradation Paths

| Missing library / flag | Result |
|---|---|
| No matplotlib | `analyze_motion()` catches `ImportError`, prints a warning, returns `(None, None)`. Rally detection proceeds on audio only. |
| `--no-motion` flag | Skips frame extraction entirely — faster but less precise rally boundaries. |
| Both (no matplotlib + `--no-motion`) | Audio-only, fastest path. Good for long practice sessions. |

## Why Not Use PIL/Pillow Instead?

The script could use `PIL.Image` for frame reading and skip matplotlib entirely for motion analysis, but:
- matplotlib is already pulled in by the `--plot` feature
- `plt.imread()` handles JPEG natively without extra imports
- The try/except guard means users who only want audio-only don't need to install anything beyond numpy

If matplotlib is ever fully removed from the dependency list, `plt.imread()` would need to be replaced with `PIL.Image.open()` or `cv2.imread()`.

## Bitrate Auto-Scale Logic

| Resolution | SDR | HDR |
|---|---|---|
| ≤1080p (≤1920px) | 8M | 12M |
| >1080p (>1920px) | 25M | 35M |

The probe checks `pix_fmt` for `10` or `p010le` and `color_primaries` for `bt2020` to detect HDR.