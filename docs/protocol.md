# ipaddisplay wire protocol — M0-M2 subset

Byte-exact contract for the handshake + keepalive. The Swift listener
(`ios/Sources/LinkServer.swift`), the Python host harness
(`host/handshake_host.py`), the C header (`host/protocol.h`), and the Python
codec (`host/protocol.py`) MUST all match this document.

## Conventions

- **One TCP connection.** iPad listens on `0.0.0.0:7000`; host connects (host =
  client, iPad = listener) over usbmux.
- **Byte order: little-endian.** Both ends are little-endian (x86-64 host,
  ARM64 iPad), so no byteswaps.
- **Packed, no implicit padding.** All explicit `_pad` fields are sent as `0`.
- Every message = a 24-byte `MsgHeader` followed by exactly `header.length`
  payload bytes. Header + payload are written as a single scatter-gather write.
- `seq` is monotonic per message (per sender). `timestampUs` is the sender's
  monotonic clock in microseconds. Both are informational for the handshake.
- Sizes below are payload sizes (the bytes after the header).

## MsgHeader — 24 bytes

| Offset | Size | Type | Field | Notes |
|-------:|-----:|------|-------|-------|
| 0  | 1 | u8  | `type`        | MessageType code |
| 1  | 1 | u8  | `flags`       | type-specific bitfield (0 for handshake) |
| 2  | 2 | u16 | `_pad`        | = 0 |
| 4  | 4 | u32 | `seq`         | monotonic per-message sequence |
| 8  | 4 | u32 | `length`      | payload bytes that follow (0..~24M) |
| 12 | 4 | u32 | `_pad2`       | = 0 (keeps `timestampUs` 8-byte aligned) |
| 16 | 8 | u64 | `timestampUs` | sender monotonic clock, microseconds |

Python struct: `"<BBHIIIQ"` (calcsize == 24).

## Message type codes

| Code | Name | Dir | Payload bytes |
|------|------|-----|--------------:|
| 0x01 | HELLO | H→D | 16 |
| 0x02 | DEVICE_INFO | D→H | 16 |
| 0x03 | STREAM_CONFIG | H→D | 12 |
| 0x04 | STREAM_CONFIG_ACK | D→H | 2 |
| 0x05 | TEARDOWN | both | 0 |
| 0x06 | PING | both | 0..N (opaque) |
| 0x07 | PONG | both | echo of the PING payload |
| 0x08 | FRAME_PRESENTED | D→H | (M9) |
| 0x09 | ERROR | both | (later) |
| 0x10 | VIDEO_PARAM_SETS | H→D | (M5) |
| 0x11 | VIDEO_FRAME | H→D | (M5) |
| 0x20 | SET_LAYER | H→D | (M8) |
| 0x21 | STILL_BEGIN | H→D | (M8) |
| 0x22 | STILL_TILE | H→D | (M8) |
| 0x23 | STILL_COMMIT | H→D | (M8) |

Only the rows with a concrete byte count below are implemented in M0-M2; the
rest are reserved codes (stubbed in the Swift dispatch).

## HELLO (H→D) — 16 bytes

| Offset | Size | Type | Field | Value |
|-------:|-----:|------|-------|-------|
| 0  | 4 | char[4] | `magic`          | `'IPDP'` (not null-terminated) |
| 4  | 2 | u16     | `protoVer`       | 1 |
| 6  | 2 | u16     | `hostFlags`      | reserved (0) |
| 8  | 4 | u32     | `codecMask`      | bit0=H264, bit1=HEVC |
| 12 | 4 | u32     | `maxBitrateKbps` | host's max offered bitrate |

Python struct: `"<4sHHII"`.

## DEVICE_INFO (D→H) — 16 bytes

| Offset | Size | Type | Field | Value |
|-------:|-----:|------|-------|-------|
| 0  | 2 | u16 | `nativeW`      | 2732 |
| 2  | 2 | u16 | `nativeH`      | 2048 |
| 4  | 2 | u16 | `maxDecodeW`   | max decodable width |
| 6  | 2 | u16 | `maxDecodeH`   | max decodable height |
| 8  | 4 | u32 | `decoderMask`  | bit0=H264, bit1=HEVC |
| 12 | 1 | u8  | `maxRefreshHz` | e.g. 120 |
| 13 | 1 | u8  | `supportsP3`   | 0/1 |
| 14 | 1 | u8  | `hwHEVC`       | 0/1 |
| 15 | 1 | u8  | `_pad`         | = 0 |

Python struct: `"<HHHHIBBBB"`.

## STREAM_CONFIG (H→D) — 12 bytes

| Offset | Size | Type | Field | Canonical value |
|-------:|-----:|------|-------|-----------------|
| 0  | 2 | u16 | `w`              | 2732 |
| 2  | 2 | u16 | `h`              | 2048 |
| 4  | 1 | u8  | `fpsCap`         | 60 |
| 5  | 1 | u8  | `codec`          | 0 = H264 |
| 6  | 1 | u8  | `chroma`         | 0 = 4:2:0 |
| 7  | 1 | u8  | `bitDepth`       | 8 |
| 8  | 1 | u8  | `fullRange`      | 1 |
| 9  | 1 | u8  | `colorPrimaries` | 12 = P3-D65 |
| 10 | 1 | u8  | `transfer`       | 13 = sRGB |
| 11 | 1 | u8  | `matrix`         | 1 = BT.709 |

Python struct: `"<HHBBBBBBBB"`.

## STREAM_CONFIG_ACK (D→H) — 2 bytes

| Offset | Size | Type | Field | Notes |
|-------:|-----:|------|-------|-------|
| 0 | 1 | u8 | `ok`     | 1 = accepted, 0 = rejected |
| 1 | 1 | u8 | `reason` | 0 on ok; error code on reject |

Python struct: `"<BB"`.

## PING / PONG (both) — 0..N bytes

Opaque echo. The receiver of a `PING` replies with a `PONG` whose payload is a
byte-for-byte copy of the `PING` payload. Used for keepalive and live RTT. The
header `seq`/`timestampUs` are how the sender measures round-trip time.

## Session FSM (M0-M2)

```
host connects (usbmux fd to iPad :7000)
        │
        ▼
host ── HELLO ──────────────► device
device ── DEVICE_INFO ──────► host
host picks config (2732x2048, H264, 4:2:0, 8-bit,
                   fullRange=1, primaries=12, transfer=13, matrix=1)
host ── STREAM_CONFIG ──────► device
device ── STREAM_CONFIG_ACK{ok=1} ► host
        │
        ▼
   keepalive loop:  host ── PING ──► device ── PONG ──► host
```

For the full pipeline (M3+) the host MUST, after `STREAM_CONFIG_ACK{ok}`, send
`VIDEO_PARAM_SETS` then a `VIDEO_FRAME(IDR)` first, and resend param sets + IDR
on every (re)connect. Those messages are out of scope for this M0-M2 contract.
