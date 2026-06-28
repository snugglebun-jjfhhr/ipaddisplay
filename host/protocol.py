"""ipaddisplay wire protocol — canonical Python codec (M0-M2 subset).

Source of truth: docs/protocol.md. Mirror of host/protocol.h.

Wire format is LITTLE-ENDIAN, packed (no implicit padding). The 24-byte
MsgHeader prefixes every message; the payload (`length` bytes) follows.

This module covers the handshake/keepalive subset fully (HELLO, DEVICE_INFO,
STREAM_CONFIG, STREAM_CONFIG_ACK, PING/PONG). Video/still types have codes in
MessageType but no payload codecs here yet.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from enum import IntEnum

# --- Message type codes ----------------------------------------------------


class MessageType(IntEnum):
    HELLO = 0x01
    DEVICE_INFO = 0x02
    STREAM_CONFIG = 0x03
    STREAM_CONFIG_ACK = 0x04
    TEARDOWN = 0x05
    PING = 0x06
    PONG = 0x07
    FRAME_PRESENTED = 0x08
    ERROR = 0x09
    VIDEO_PARAM_SETS = 0x10
    VIDEO_FRAME = 0x11
    SET_LAYER = 0x20
    STILL_BEGIN = 0x21
    STILL_TILE = 0x22
    STILL_COMMIT = 0x23


# --- Codec bit masks (HELLO.codecMask / DEVICE_INFO.decoderMask) ------------

CODEC_H264 = 1 << 0
CODEC_HEVC = 1 << 1

# --- MsgHeader (24 bytes) --------------------------------------------------
# offset 0  u8  type
# offset 1  u8  flags
# offset 2  u16 _pad   (=0)
# offset 4  u32 seq
# offset 8  u32 length (payload bytes that follow)
# offset 12 u32 _pad2  (=0)
# offset 16 u64 timestampUs
HEADER_FORMAT = "<BBHIIIQ"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
assert HEADER_SIZE == 24, f"MsgHeader must be 24 bytes, got {HEADER_SIZE}"


@dataclass
class MsgHeader:
    type: int
    flags: int = 0
    seq: int = 0
    length: int = 0
    timestamp_us: int = 0

    def pack(self) -> bytes:
        return struct.pack(
            HEADER_FORMAT,
            int(self.type),
            self.flags,
            0,  # _pad
            self.seq,
            self.length,
            0,  # _pad2
            self.timestamp_us,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "MsgHeader":
        if len(data) != HEADER_SIZE:
            raise ValueError(f"MsgHeader needs {HEADER_SIZE} bytes, got {len(data)}")
        type_, flags, _pad, seq, length, _pad2, ts = struct.unpack(HEADER_FORMAT, data)
        return cls(type=type_, flags=flags, seq=seq, length=length, timestamp_us=ts)


def now_us() -> int:
    """Monotonic microsecond clock for header timestamps."""
    return int(time.monotonic() * 1_000_000)


def make_header(
    type: int, payload_len: int, seq: int = 0, flags: int = 0, timestamp_us: int | None = None
) -> bytes:
    """Convenience: build a packed header for a message with `payload_len` bytes."""
    return MsgHeader(
        type=type,
        flags=flags,
        seq=seq,
        length=payload_len,
        timestamp_us=now_us() if timestamp_us is None else timestamp_us,
    ).pack()


# --- HELLO (H->D), 16 bytes ------------------------------------------------
# offset 0  char magic[4] = 'IPDP'
# offset 4  u16  protoVer = 1
# offset 6  u16  hostFlags
# offset 8  u32  codecMask  (bit0=H264, bit1=HEVC)
# offset 12 u32  maxBitrateKbps
HELLO_FORMAT = "<4sHHII"
HELLO_SIZE = struct.calcsize(HELLO_FORMAT)
assert HELLO_SIZE == 16, f"HELLO must be 16 bytes, got {HELLO_SIZE}"

HELLO_MAGIC = b"IPDP"
PROTO_VERSION = 1


@dataclass
class Hello:
    proto_ver: int = PROTO_VERSION
    host_flags: int = 0
    codec_mask: int = CODEC_H264
    max_bitrate_kbps: int = 80_000

    def pack(self) -> bytes:
        return struct.pack(
            HELLO_FORMAT,
            HELLO_MAGIC,
            self.proto_ver,
            self.host_flags,
            self.codec_mask,
            self.max_bitrate_kbps,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "Hello":
        magic, proto_ver, host_flags, codec_mask, max_bitrate = struct.unpack(
            HELLO_FORMAT, data
        )
        if magic != HELLO_MAGIC:
            raise ValueError(f"bad HELLO magic: {magic!r}")
        return cls(
            proto_ver=proto_ver,
            host_flags=host_flags,
            codec_mask=codec_mask,
            max_bitrate_kbps=max_bitrate,
        )


# --- DEVICE_INFO (D->H), 16 bytes ------------------------------------------
# offset 0  u16 nativeW = 2732
# offset 2  u16 nativeH = 2048
# offset 4  u16 maxDecodeW
# offset 6  u16 maxDecodeH
# offset 8  u32 decoderMask (bit0=H264, bit1=HEVC)
# offset 12 u8  maxRefreshHz
# offset 13 u8  supportsP3
# offset 14 u8  hwHEVC
# offset 15 u8  _pad = 0
DEVICE_INFO_FORMAT = "<HHHHIBBBB"
DEVICE_INFO_SIZE = struct.calcsize(DEVICE_INFO_FORMAT)
assert DEVICE_INFO_SIZE == 16, f"DEVICE_INFO must be 16 bytes, got {DEVICE_INFO_SIZE}"


@dataclass
class DeviceInfo:
    native_w: int = 2732
    native_h: int = 2048
    max_decode_w: int = 2732
    max_decode_h: int = 2048
    decoder_mask: int = CODEC_H264
    max_refresh_hz: int = 120
    supports_p3: int = 1
    hw_hevc: int = 0

    def pack(self) -> bytes:
        return struct.pack(
            DEVICE_INFO_FORMAT,
            self.native_w,
            self.native_h,
            self.max_decode_w,
            self.max_decode_h,
            self.decoder_mask,
            self.max_refresh_hz,
            self.supports_p3,
            self.hw_hevc,
            0,  # _pad
        )

    @classmethod
    def unpack(cls, data: bytes) -> "DeviceInfo":
        (
            native_w,
            native_h,
            max_decode_w,
            max_decode_h,
            decoder_mask,
            max_refresh_hz,
            supports_p3,
            hw_hevc,
            _pad,
        ) = struct.unpack(DEVICE_INFO_FORMAT, data)
        return cls(
            native_w=native_w,
            native_h=native_h,
            max_decode_w=max_decode_w,
            max_decode_h=max_decode_h,
            decoder_mask=decoder_mask,
            max_refresh_hz=max_refresh_hz,
            supports_p3=supports_p3,
            hw_hevc=hw_hevc,
        )


# --- STREAM_CONFIG (H->D), 12 bytes ----------------------------------------
# offset 0  u16 W
# offset 2  u16 H
# offset 4  u8  fpsCap
# offset 5  u8  codec          (0=H264)
# offset 6  u8  chroma         (0=420)
# offset 7  u8  bitDepth       (8)
# offset 8  u8  fullRange      (1)
# offset 9  u8  colorPrimaries (12=P3-D65)
# offset 10 u8  transfer       (13=sRGB)
# offset 11 u8  matrix         (1=BT.709)
STREAM_CONFIG_FORMAT = "<HHBBBBBBBB"
STREAM_CONFIG_SIZE = struct.calcsize(STREAM_CONFIG_FORMAT)
assert STREAM_CONFIG_SIZE == 12, f"STREAM_CONFIG must be 12 bytes, got {STREAM_CONFIG_SIZE}"

CODEC_ID_H264 = 0
CHROMA_420 = 0


@dataclass
class StreamConfig:
    w: int = 2732
    h: int = 2048
    fps_cap: int = 60
    codec: int = CODEC_ID_H264
    chroma: int = CHROMA_420
    bit_depth: int = 8
    full_range: int = 1
    color_primaries: int = 12  # P3-D65
    transfer: int = 13  # sRGB
    matrix: int = 1  # BT.709

    def pack(self) -> bytes:
        return struct.pack(
            STREAM_CONFIG_FORMAT,
            self.w,
            self.h,
            self.fps_cap,
            self.codec,
            self.chroma,
            self.bit_depth,
            self.full_range,
            self.color_primaries,
            self.transfer,
            self.matrix,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "StreamConfig":
        (
            w,
            h,
            fps_cap,
            codec,
            chroma,
            bit_depth,
            full_range,
            color_primaries,
            transfer,
            matrix,
        ) = struct.unpack(STREAM_CONFIG_FORMAT, data)
        return cls(
            w=w,
            h=h,
            fps_cap=fps_cap,
            codec=codec,
            chroma=chroma,
            bit_depth=bit_depth,
            full_range=full_range,
            color_primaries=color_primaries,
            transfer=transfer,
            matrix=matrix,
        )


# --- STREAM_CONFIG_ACK (D->H), 2 bytes -------------------------------------
# offset 0  u8 ok
# offset 1  u8 reason
STREAM_CONFIG_ACK_FORMAT = "<BB"
STREAM_CONFIG_ACK_SIZE = struct.calcsize(STREAM_CONFIG_ACK_FORMAT)
assert STREAM_CONFIG_ACK_SIZE == 2, f"STREAM_CONFIG_ACK must be 2 bytes, got {STREAM_CONFIG_ACK_SIZE}"


@dataclass
class StreamConfigAck:
    ok: int = 1
    reason: int = 0

    def pack(self) -> bytes:
        return struct.pack(STREAM_CONFIG_ACK_FORMAT, self.ok, self.reason)

    @classmethod
    def unpack(cls, data: bytes) -> "StreamConfigAck":
        ok, reason = struct.unpack(STREAM_CONFIG_ACK_FORMAT, data)
        return cls(ok=ok, reason=reason)


# --- self-test -------------------------------------------------------------

if __name__ == "__main__":
    assert struct.calcsize(HEADER_FORMAT) == 24

    # Header round-trip + offset checks.
    h = MsgHeader(type=MessageType.STREAM_CONFIG, flags=0x02, seq=7, length=12, timestamp_us=123456)
    raw = h.pack()
    assert len(raw) == 24
    assert raw[0] == int(MessageType.STREAM_CONFIG)
    assert raw[1] == 0x02
    assert raw[2:4] == b"\x00\x00"  # _pad
    assert struct.unpack_from("<I", raw, 4)[0] == 7  # seq
    assert struct.unpack_from("<I", raw, 8)[0] == 12  # length
    assert raw[12:16] == b"\x00\x00\x00\x00"  # _pad2
    assert struct.unpack_from("<Q", raw, 16)[0] == 123456  # timestampUs
    back = MsgHeader.unpack(raw)
    assert back.type == h.type and back.seq == 7 and back.length == 12
    assert back.flags == 0x02 and back.timestamp_us == 123456

    # HELLO round-trip.
    hello = Hello(host_flags=0xABCD, codec_mask=CODEC_H264, max_bitrate_kbps=80_000)
    hr = hello.pack()
    assert len(hr) == 16
    assert hr[0:4] == b"IPDP"
    assert struct.unpack_from("<H", hr, 4)[0] == 1  # protoVer
    assert Hello.unpack(hr) == hello

    # DEVICE_INFO round-trip + native res offsets.
    di = DeviceInfo()
    dr = di.pack()
    assert len(dr) == 16
    assert struct.unpack_from("<H", dr, 0)[0] == 2732
    assert struct.unpack_from("<H", dr, 2)[0] == 2048
    assert dr[15] == 0  # _pad
    assert DeviceInfo.unpack(dr) == di

    # STREAM_CONFIG round-trip + the canonical handshake config.
    sc = StreamConfig()
    sr = sc.pack()
    assert len(sr) == 12
    assert struct.unpack_from("<H", sr, 0)[0] == 2732
    assert struct.unpack_from("<H", sr, 2)[0] == 2048
    assert sr[8] == 1  # fullRange
    assert sr[9] == 12  # colorPrimaries P3-D65
    assert sr[10] == 13  # transfer sRGB
    assert sr[11] == 1  # matrix BT.709
    assert StreamConfig.unpack(sr) == sc

    # STREAM_CONFIG_ACK round-trip.
    ack = StreamConfigAck(ok=1, reason=0)
    ar = ack.pack()
    assert len(ar) == 2
    assert StreamConfigAck.unpack(ar) == ack

    print("protocol.py self-test OK: header=24B hello=16B device_info=16B "
          "stream_config=12B ack=2B; all round-trips pass.")
