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
| 0x10 | VIDEO_PARAM_SETS | H→D | 1 + Σ(2 + nalLen) |
| 0x11 | VIDEO_FRAME | H→D | Σ(4 + nalLen) (one AVCC access unit) |
| 0x20 | SET_LAYER | H→D | (M8) |
| 0x21 | STILL_BEGIN | H→D | (M8) |
| 0x22 | STILL_TILE | H→D | (M8) |
| 0x23 | STILL_COMMIT | H→D | (M8) |

Rows with a concrete byte count are implemented in M0-M2 (handshake) and M5
(VIDEO_PARAM_SETS, VIDEO_FRAME); the rest are reserved codes (stubbed in the
Swift dispatch).

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
| 9  | 1 | u8  | `colorPrimaries` | **1 = BT.709** (reconciled, see note) |
| 10 | 1 | u8  | `transfer`       | 13 = sRGB / iec61966-2-1 |
| 11 | 1 | u8  | `matrix`         | 1 = BT.709 |

Python struct: `"<HHBBBBBBBB"`.

**M5 reconciliation — `colorPrimaries` is 1 (BT.709), not 12 (P3-D65).** The live
M3/M4 NVENC stream signals VUI `colour_primaries=1`, `transfer_characteristics=13`,
`matrix_coefficients=1`, `video_full_range_flag=1`. The original M0/M2 handshake
test wrongly sent `12`; the wire now matches the bitstream. The host **MUST** send
`colorPrimaries=1, transfer=13, matrix=1, fullRange=1, codec=0, chroma=0, bitDepth=8`.
The iPad still **color-manages** the BT.709/sRGB signal onto the Display-P3 panel at
render time (it sets `CAMetalLayer.colorspace` to sRGB and writes `.bgra8Unorm` so
Core Animation converts sRGB→P3) — the panel is P3 even though the signalled
primaries are 709. Do **not** tag the layer Display-P3 while writing sRGB values.

## STREAM_CONFIG_ACK (D→H) — 2 bytes

| Offset | Size | Type | Field | Notes |
|-------:|-----:|------|-------|-------|
| 0 | 1 | u8 | `ok`     | 1 = accepted, 0 = rejected |
| 1 | 1 | u8 | `reason` | 0 on ok; error code on reject |

Python struct: `"<BB"`.

## M5 video messages — NAL length endianness

The MsgHeader and every handshake struct above are **little-endian**. The video
messages below are the one **documented exception**: their per-NAL **length
prefixes are big-endian (network byte order)**, following the ISO-14496-15 AVCC /
`avcC` convention. This lets the iPad wrap a `VIDEO_FRAME` payload directly in a
`CMBlockBuffer` and decode it with `nalUnitHeaderLength = 4` **without rewriting
any bytes**. The payload is an opaque CoreMedia container, not a packed struct.

ffmpeg/NVENC emit **Annex-B** (start codes `00 00 01` / `00 00 00 01` between
NALs, AUD type-9 present, in-band SPS/PPS repeated). The **host** converts
Annex-B → AVCC and extracts SPS/PPS; the iPad never scans for AU boundaries
across socket reads — it receives whole access units.

H.264 NAL types used: `5` = IDR slice, `7` = SPS, `8` = PPS, `9` = AUD.

## VIDEO_PARAM_SETS (H→D) — `1 + Σ(2 + nalLen)` bytes

| Offset | Size | Type | Field | Notes |
|-------:|-----:|------|-------|-------|
| 0 | 1 | u8 | `count` | number of parameter-set NALs |
| 1 | … | — | NAL records | `count` repetitions of the record below |

Per-NAL record (repeated `count` times), **SPS (type 7) first, then PPS (type 8)**:

| Size | Type | Field | Notes |
|-----:|------|-------|-------|
| 2 | u16 (**big-endian**) | `nalLen` | length of the raw NAL |
| `nalLen` | bytes | `nal` | raw NAL bytes, **no start code, no Annex-B prefix** |

iPad: feed these directly to `CMVideoFormatDescriptionCreateFromH264ParameterSets`
(`nalUnitHeaderLength: 4`). Sent on first connect and on any param-set change; the
host resends param sets + an IDR on every (re)connect.

## VIDEO_FRAME (H→D) — `Σ(4 + nalLen)` bytes

Payload is exactly one access unit in **AVCC** form: a back-to-back sequence of
NAL records covering the whole AU (AUD/SEI/SPS/PPS/slice NALs as present).

Per-NAL record (repeated to fill the payload):

| Size | Type | Field | Notes |
|-----:|------|-------|-------|
| 4 | u32 (**big-endian**) | `nalLen` | length of the raw NAL |
| `nalLen` | bytes | `nal` | raw NAL bytes, no start code |

`MsgHeader.flags` bitfield for VIDEO_FRAME:

| Bit | Mask | Name | Meaning |
|----:|-----:|------|---------|
| 0 | 0x01 | `IDR` | AU contains an IDR slice (NAL type 5) — keyframe |
| 1 | 0x02 | `paramSetsPrecede` | AU carries in-band SPS/PPS |
| 2 | 0x04 | `discardable` | AU is non-reference / droppable (unused: I/P-only) |

iPad: the payload bytes are a ready-to-decode AVCC access unit — wrap in a
`CMBlockBuffer`, pair with the param-set format description, and submit to the
`VTDecompressionSession`. No NAL scanning required.

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
host picks config (H264, 4:2:0, 8-bit,
                   fullRange=1, primaries=1, transfer=13, matrix=1)
host ── STREAM_CONFIG ──────► device
device ── STREAM_CONFIG_ACK{ok=1} ► host
        │
        ▼
   keepalive loop:  host ── PING ──► device ── PONG ──► host
```

For the full pipeline (M5+) the host MUST, after `STREAM_CONFIG_ACK{ok}`, send
`VIDEO_PARAM_SETS` then a `VIDEO_FRAME(IDR)` first, and resend param sets + IDR
on every (re)connect. See the M5 video message sections above for their byte
layouts.
