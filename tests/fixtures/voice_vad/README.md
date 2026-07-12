# Voice VAD Calibration Fixtures

`py-webrtcvad-test-audio.raw` is the upstream `py-webrtcvad` speech fixture at
commit `e283ca41df3a84b0e87fb1f5cb9b21580a286b09`:

https://github.com/wiseman/py-webrtcvad/blob/e283ca41df3a84b0e87fb1f5cb9b21580a286b09/test-audio.raw

The file is 8 kHz, mono, signed 16-bit little-endian PCM. Its SHA-256 is
`3dbb730b90e0266d78d4fe03f00aba37a83780a6a5240d9084b5c01266194c1e`.
The upstream mode-2 test publishes this 30 ms frame classification:

```text
000000 11111 11111 11111 11111 0000
```

Spaces are included only for readability. The pattern labels voiced frames 6
through 25, or approximately 180-780 ms. After a
Twilio mu-law round trip and 20 ms framing, Kevin's current mode-2 tracker
starts at 40 ms and ends at 800 ms. The early start is a known false-positive
bias in this one fixture; the completed endpoint is 20 ms later than the
upstream label. Tests preserve these values as calibration evidence, not as a
claim that one English sample validates production VAD quality.

The fixture is upstream test data, contains no Kevin customer audio or PII, and
is redistributed under the notices in `LICENSE.py-webrtcvad`.
