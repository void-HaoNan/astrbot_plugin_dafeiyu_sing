"""用 RVC 整合包自带的 librosa 估一段人声的中位基频，给自动升降调用。

  <RVC runtime>\\python.exe pitch_probe.py vocals.wav

输出一行浮点数（Hz），估不出来输出 0。
上限压到 320Hz，避免男声被估成高八度。
"""

from __future__ import annotations

import sys

import numpy as np


def main() -> int:
    try:
        import librosa

        y, sr = librosa.load(sys.argv[1], sr=16000, mono=True, duration=30)
        if len(y) < sr:
            print(0.0)
            return 0
        f0 = librosa.yin(y, fmin=70, fmax=320, sr=sr, frame_length=1024)
        vals = f0[np.isfinite(f0) & (f0 > 70) & (f0 < 320)]
        if not len(vals):
            print(0.0)
            return 0
        low = float(np.percentile(vals, 25))
        med = float(np.median(vals))
        # 明显的八度误判：中位数比低分位高一倍以上，就折回来
        if med > 1.8 * low and med / 2 >= 70:
            med /= 2
        print(round(med, 1))
    except Exception:  # noqa: BLE001
        print(0.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
