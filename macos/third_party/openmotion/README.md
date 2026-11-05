# OpenMotion bring-up sequence

`bringup_seq.txt` comes unchanged from [coleman-sagil/OpenMotion](https://github.com/coleman-sagil/OpenMotion), `rust-driver/bringup_seq.txt` on `main`, downloaded 2026-11-04. It is covered by the Boost Software License in `LICENSE`.

It holds 358 USB control writes that OpenMotion recorded from Leap's own `leapd` service, on a device with firmware 1.7.0. Each row is:

```
bmRequestType bRequest wValue wIndex dataHex
```

`dataHex` is `-` when a row has no data. The file has no timing information.

**Caution:** 16 rows write high-entropy bytes through the saturation control to addresses 16 to 19, 32 to 39 and 44 to 47. Another recording of leapd, in [wtfuzz/leapuvc](https://github.com/wtfuzz/leapuvc), writes different bytes to the same addresses, so these look like an authentication response tied to one device or session. Nobody has established whether those addresses are persistent. Don't assume this replay is safe or useful on another Leap.
