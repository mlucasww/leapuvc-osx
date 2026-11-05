# OpenMotion bring-up sequence

`bringup_seq.txt` comes unchanged from [coleman-sagil/OpenMotion](https://github.com/coleman-sagil/OpenMotion), `rust-driver/bringup_seq.txt` on `main`, downloaded 2026-11-04. It is covered by the Boost Software License in `LICENSE`.

It holds 358 USB control writes that OpenMotion recorded from Leap's own `leapd` service, on a device with firmware 1.7.0. Each row is:

```
bmRequestType bRequest wValue wIndex dataHex
```

`dataHex` is `-` when a row has no data. The file has no timing information.
