# Deskbot firmware

This directory contains the USB-only firmware for the Deskbot v2 custom
ESP32-S3 board. A Seeed Studio XIAO ESP32S3 Sense profile remains available
for explicit compatibility builds, but it is not the product default.

Every robot uses the same firmware image. The device ID is derived from the
factory eFuse base MAC, so no deployment address, PC credential, or per-device
configuration is compiled into the image.

The same image can attach to any compatible self-hosted PC service. Product
data stays in that PC's single local workspace while the device is connected;
the firmware is never rebuilt for a particular PC service.

## Connection model

The robot communicates only with the locally attached PC service over its USB
CDC serial port. The PC owns all remote service access. The firmware does not
make an independent network connection.

The DBOT v1 serial protocol uses a CRC-protected 24-byte header, payload CRC,
monotonic sequence numbers, a random session epoch, hello/heartbeat lifecycle,
and these isolated channels:

- control JSON;
- PB JSON and PB binary media;
- microphone Opus uplink (batched 3 x 20 ms frames, 60 ms per USB frame);
- speaker Opus downlink;
- camera JPEG uplink;
- framed device logs.

The PC must complete the hello handshake before media or logs can be sent. A
heartbeat timeout, epoch mismatch, malformed frame, or repeated critical write
failure invalidates the session and clears in-flight media. When the firmware
tears a session down itself while the link is still writable, it sends a
`session_end` notice so the host reconnects immediately instead of waiting for
the heartbeat timeout.

The local control-JSON command surface is reduced to read-only queries and
maintenance: `head_pos` (position readout), `task` (task/CPU diagnostics dump)
and `reboot`/`restart`. The legacy factory/action gesture command layer is
removed; servo motion only runs through the PB `servo[]` timeline.

## Build

Install PlatformIO, then run:

```bash
cd hardware
pio run -e deskbot_v2
```

The pinned ESP32-S3 board configuration enables CDC at boot with
`ARDUINO_USB_CDC_ON_BOOT=1` and selects the ESP32-S3 USB serial/JTAG CDC mode
with `ARDUINO_USB_MODE=1`. To build the retained XIAO compatibility profile,
select `-e seeed_xiao_esp32s3` explicitly.

The fixed compression gain of the ESP-SR WebRTC AGC is tunable at build time
via `-DDESKBOT_AFE_AGC_COMPRESSION_GAIN_DB=<n>` (default 15 dB). It covers the
cold-start window where the adaptive AGC stage has not yet converged, so the
first utterances after connecting are loud enough to transcribe.

## USB enumeration and diagnosis

The expected runtime USB device is Espressif `303A:1001`. This is the
ESP32-S3 hardware USB Serial/JTAG endpoint; `2886:0056` would indicate the
different TinyUSB device mode and is not used by this firmware.

A hard reset or the reset following an upload disconnects the COM/tty device
briefly while Windows, macOS or Linux enumerates it again. Diagnose these as
different states:

- no COM/tty device: enumeration, cable/power, driver, reset loop or invalid
  image problem;
- COM/tty exists but the service remains disconnected: DBOT hello/session
  problem; inspect the PC service log without erasing or reflashing first.

## Flash and monitor

On Linux or macOS:

```bash
cd hardware
./flash_rom.sh build
./flash_rom.sh upload /dev/ttyACM0
./flash_rom.sh log /dev/ttyACM0
```

Use the equivalent PlatformIO upload command on Windows. For a device that
previously ran an older image, erase the complete flash before the first
USB-only installation so stale NVS settings cannot survive.

After flashing, keep the robot connected by USB and start the PC service.
While no PC service is connected the display shows the standby screen: the
built-in default face with a `请连接PC服务` ("please connect the PC service")
banner. The DBOT hello clears it and hands the screen to the PC expression
system; it returns when the session ends. The standby screen only paints
pixels and never enters the PB expression state, interpolation baselines or
the display CRC.

## Partition layout

The 8 MiB layout contains NVS, one factory application, coredump storage, and
FFat. It deliberately has no alternate application slot or update metadata.

See `service/docs/esp32_pb_protocol.md` for the PB payload contract and the
serial transport implementation under
`service/src/deskbot_server/infrastructure/serial/` for the PC endpoint.
