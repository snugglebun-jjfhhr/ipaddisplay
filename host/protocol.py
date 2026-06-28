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
# offset 9  u8  colorPrimaries (1=BT.709)  -- RECONCILED, see note below
# offset 10 u8  transfer       (13=sRGB/iec61966-2-1)
# offset 11 u8  matrix         (1=BT.709)
#
# RECONCILIATION (M5): colorPrimaries is 1 (BT.709), NOT 12 (P3-D65). The live
# M3/M4 NVENC stream signals VUI colour_primaries=1, transfer=13, matrix=1,
# full-range. The M0/M2 handshake test wrongly used 12; the wire now matches the
# bitstream. The iPad still color-MANAGES BT.709/sRGB -> the Display-P3 panel at
# render time (CAMetalLayer.colorspace = sRGB), so the panel is P3 even though
# the signalled primaries are 709.
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
    color_primaries: int = 1  # BT.709 (RECONCILED from 12; stream is 709/sRGB)
    transfer: int = 13  # sRGB / iec61966-2-1
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


# --- M5 video: NAL helpers + VIDEO_PARAM_SETS / VIDEO_FRAME ----------------
#
# ENDIANNESS NOTE (deliberate, documented exception to the all-LE wire):
# the NAL *length prefixes* inside the video messages are BIG-ENDIAN (network
# byte order), per the ISO-14496-15 AVCC / `avcC` convention. This lets the iPad
# wrap a VIDEO_FRAME payload directly in a CMBlockBuffer and decode it with
# nalUnitHeaderLength=4 WITHOUT rewriting any bytes. The MsgHeader and all the
# handshake structs above remain little-endian; only these NAL length prefixes
# are big-endian, because the VIDEO_FRAME payload is an opaque CoreMedia
# container, not a packed struct.
#
# Annex-B (what ffmpeg/NVENC emits) uses start codes (00 00 01 / 00 00 00 01)
# between NALs. The host converts to AVCC (length-prefixed) before sending.

# H.264 NAL unit types (nal_unit_header byte & 0x1F).
NAL_SLICE_NON_IDR = 1
NAL_SLICE_IDR = 5
NAL_SEI = 6
NAL_SPS = 7
NAL_PPS = 8
NAL_AUD = 9

# VIDEO_FRAME header flag bits (carried in MsgHeader.flags).
VIDEO_FLAG_IDR = 1 << 0              # AU contains an IDR slice (NAL type 5)
VIDEO_FLAG_PARAM_SETS_PRECEDE = 1 << 1  # AU carries in-band SPS/PPS
VIDEO_FLAG_DISCARDABLE = 1 << 2     # AU is non-reference / droppable


def nal_type(nal: bytes) -> int:
    """H.264 NAL unit type of a single raw NAL (no start code, no length prefix)."""
    if not nal:
        raise ValueError("empty NAL")
    return nal[0] & 0x1F


def _find_start_codes(data: bytes) -> list[tuple[int, int]]:
    """Locate Annex-B start codes. Returns [(sc_start, payload_start), ...] in
    order, recognizing both 3-byte (00 00 01) and 4-byte (00 00 00 01) prefixes.
    """
    out: list[tuple[int, int]] = []
    i, n = 0, len(data)
    while i + 3 <= n:
        if data[i] == 0 and data[i + 1] == 0:
            if data[i + 2] == 1:
                out.append((i, i + 3))
                i += 3
                continue
            if data[i + 2] == 0 and i + 3 < n and data[i + 3] == 1:
                out.append((i, i + 4))
                i += 4
                continue
        i += 1
    return out


def split_nals(annexb: bytes) -> list[bytes]:
    """Split an Annex-B byte stream into raw NAL units (start codes removed).

    Trailing zero bytes between a NAL and the next start code (Annex-B
    trailing_zero_8bits / leading zero_byte) are stripped so each returned NAL
    is exactly its nal_unit bytes.
    """
    positions = _find_start_codes(annexb)
    nals: list[bytes] = []
    for idx, (_sc, payload_start) in enumerate(positions):
        end = positions[idx + 1][0] if idx + 1 < len(positions) else len(annexb)
        nal = annexb[payload_start:end]
        # Drop framing zero padding that precedes the next start code.
        nal = nal.rstrip(b"\x00")
        if nal:
            nals.append(nal)
    return nals


def avcc_from_nals(nals: list[bytes]) -> bytes:
    """Concatenate raw NALs into AVCC form: per NAL [u32 BE length][bytes]."""
    out = bytearray()
    for nal in nals:
        out += struct.pack(">I", len(nal))
        out += nal
    return bytes(out)


def annexb_to_avcc(au_bytes: bytes) -> bytes:
    """Convert one Annex-B access unit to AVCC (4-byte big-endian length prefix
    per NAL, start codes removed)."""
    return avcc_from_nals(split_nals(au_bytes))


# NAL types VideoToolbox should be fed in a VIDEO_FRAME: the VCL slices plus SEI.
_DECODE_KEEP_NAL_TYPES = (NAL_SLICE_NON_IDR, NAL_SLICE_IDR, NAL_SEI)


def avcc_from_au_for_decode(au_annexb: bytes) -> bytes:
    """Build the VIDEO_FRAME AVCC payload from an Annex-B access unit, keeping
    ONLY the NALs VideoToolbox should decode:
      - type 1  non-IDR slice  (kept)
      - type 5  IDR slice      (kept)
      - type 6  SEI            (kept)
    and dropping:
      - type 9  AUD            (VideoToolbox must not see the access-unit delimiter)
      - type 7  SPS / type 8 PPS (delivered out-of-band via VIDEO_PARAM_SETS;
                                   never duplicated in the frame)
    Other NAL types are dropped as well. Returns AVCC ([u32 BE len][nal]...)."""
    keep = [nal for nal in split_nals(au_annexb)
            if nal_type(nal) in _DECODE_KEEP_NAL_TYPES]
    return avcc_from_nals(keep)


def avcc_to_nals(avcc: bytes) -> list[bytes]:
    """Inverse of avcc_from_nals: parse AVCC (u32 BE length prefixes) into NALs."""
    nals: list[bytes] = []
    i, n = 0, len(avcc)
    while i < n:
        if i + 4 > n:
            raise ValueError("truncated AVCC length prefix")
        (ln,) = struct.unpack_from(">I", avcc, i)
        i += 4
        if i + ln > n:
            raise ValueError("AVCC NAL length overruns buffer")
        nals.append(avcc[i:i + ln])
        i += ln
    return nals


def split_access_units(annexb: bytes) -> list[bytes]:
    """Split a continuous Annex-B stream into access units at AUD (type 9)
    boundaries. Each returned element is a contiguous Annex-B slice (start codes
    preserved) beginning at its access-unit delimiter. A new AU starts at every
    AUD after the first NAL; if the stream does not begin with an AUD, the
    leading NALs form the first AU.
    """
    positions = _find_start_codes(annexb)
    if not positions:
        return []
    types = [annexb[ps] & 0x1F for (_sc, ps) in positions]
    boundaries = [0]
    for idx in range(1, len(positions)):
        if types[idx] == NAL_AUD:
            boundaries.append(idx)
    aus: list[bytes] = []
    for b_i, start_idx in enumerate(boundaries):
        sc_start = positions[start_idx][0]
        end = positions[boundaries[b_i + 1]][0] if b_i + 1 < len(boundaries) else len(annexb)
        aus.append(annexb[sc_start:end])
    return aus


def video_frame_flags(nals: list[bytes]) -> int:
    """Compute MsgHeader.flags for a VIDEO_FRAME from its NALs:
    IDR bit if any IDR slice (type 5); paramSetsPrecede bit if SPS/PPS in-band.
    Discardable is left 0 (the I/P-only stream has no droppable AUs).
    """
    flags = 0
    for nal in nals:
        t = nal_type(nal)
        if t == NAL_SLICE_IDR:
            flags |= VIDEO_FLAG_IDR
        elif t in (NAL_SPS, NAL_PPS):
            flags |= VIDEO_FLAG_PARAM_SETS_PRECEDE
    return flags


# VIDEO_PARAM_SETS (0x10), H->D
# offset 0  u8 count
# then per NAL: u16 nalLen (BIG-ENDIAN) + nalLen raw NAL bytes (no start code).
# Order: SPS (type 7) first, then PPS (type 8).
def pack_video_param_sets(param_nals: list[bytes]) -> bytes:
    """Build the VIDEO_PARAM_SETS payload from raw SPS/PPS NALs (no start codes)."""
    if len(param_nals) > 255:
        raise ValueError("too many parameter-set NALs (count is u8)")
    out = bytearray()
    out.append(len(param_nals))
    for nal in param_nals:
        if len(nal) > 0xFFFF:
            raise ValueError("param-set NAL exceeds u16 length")
        out += struct.pack(">H", len(nal))
        out += nal
    return bytes(out)


def unpack_video_param_sets(payload: bytes) -> list[bytes]:
    """Parse a VIDEO_PARAM_SETS payload into a list of raw NAL bytes."""
    if not payload:
        raise ValueError("empty VIDEO_PARAM_SETS payload")
    count = payload[0]
    nals: list[bytes] = []
    i = 1
    n = len(payload)
    for _ in range(count):
        if i + 2 > n:
            raise ValueError("truncated VIDEO_PARAM_SETS length prefix")
        (ln,) = struct.unpack_from(">H", payload, i)
        i += 2
        if i + ln > n:
            raise ValueError("VIDEO_PARAM_SETS NAL length overruns buffer")
        nals.append(payload[i:i + ln])
        i += ln
    return nals


# VIDEO_FRAME (0x11), H->D
# payload = one access unit in AVCC form: repeated [u32 BE nalLen][nal bytes].
# Flags (in MsgHeader.flags): bit0=IDR, bit1=paramSetsPrecede, bit2=discardable.
def pack_video_frame(avcc_au: bytes) -> bytes:
    """The VIDEO_FRAME payload IS the AVCC access unit; returned as-is for
    symmetry with the other pack_* helpers. Use annexb_to_avcc() to build it
    from ffmpeg/NVENC Annex-B output."""
    return avcc_au


def unpack_video_frame(payload: bytes) -> list[bytes]:
    """Parse a VIDEO_FRAME payload (AVCC) into its constituent raw NALs."""
    return avcc_to_nals(payload)


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
    assert sr[9] == 1  # colorPrimaries BT.709 (RECONCILED from 12)
    assert sr[10] == 13  # transfer sRGB
    assert sr[11] == 1  # matrix BT.709
    assert StreamConfig.unpack(sr) == sc

    # STREAM_CONFIG_ACK round-trip.
    ack = StreamConfigAck(ok=1, reason=0)
    ar = ack.pack()
    assert len(ar) == 2
    assert StreamConfigAck.unpack(ar) == ack

    # --- M5 video: NAL helpers ---------------------------------------------
    SC4 = b"\x00\x00\x00\x01"
    SC3 = b"\x00\x00\x01"
    # Synthetic NALs (first byte encodes the type in the low 5 bits).
    sps = bytes([0x67, 0x42, 0xC0, 0x1F])  # type 7
    pps = bytes([0x68, 0xCE, 0x3C, 0x80])  # type 8
    aud = bytes([0x09, 0x10])              # type 9
    # Real NALs end with a non-zero rbsp_stop byte; split_nals strips framing
    # trailing zeros, so synthetic NALs must not end in 0x00.
    idr = bytes([0x65, 0x88, 0x84, 0x80])  # type 5
    nonidr = bytes([0x41, 0x9A, 0x80])     # type 1

    assert nal_type(sps) == NAL_SPS
    assert nal_type(pps) == NAL_PPS
    assert nal_type(aud) == NAL_AUD
    assert nal_type(idr) == NAL_SLICE_IDR
    assert nal_type(nonidr) == NAL_SLICE_NON_IDR

    # split_nals: mixed 3- and 4-byte start codes, with trailing zero padding.
    stream = SC4 + aud + SC3 + sps + SC4 + pps + b"\x00\x00"
    nals = split_nals(stream)
    assert nals == [aud, sps, pps], nals

    # Annex-B -> AVCC round trip on an access unit.
    au_annexb = SC4 + aud + SC3 + idr
    avcc = annexb_to_avcc(au_annexb)
    assert avcc == struct.pack(">I", len(aud)) + aud + struct.pack(">I", len(idr)) + idr
    assert avcc_to_nals(avcc) == [aud, idr]
    # u32 length prefix is BIG-ENDIAN (AVCC): first 4 bytes encode len(aud)=2.
    assert avcc[0:4] == b"\x00\x00\x00\x02"

    # VIDEO_PARAM_SETS pack/unpack round trip (SPS then PPS).
    ps_payload = pack_video_param_sets([sps, pps])
    assert ps_payload[0] == 2  # count
    assert ps_payload[1:3] == struct.pack(">H", len(sps))  # big-endian u16
    assert unpack_video_param_sets(ps_payload) == [sps, pps]

    # VIDEO_FRAME pack/unpack round trip + flag computation.
    au_idr = SC4 + aud + SC3 + sps + SC4 + pps + SC4 + idr
    nals_idr = split_nals(au_idr)
    vf_payload = pack_video_frame(annexb_to_avcc(au_idr))
    assert unpack_video_frame(vf_payload) == nals_idr
    flags = video_frame_flags(nals_idr)
    assert flags & VIDEO_FLAG_IDR
    assert flags & VIDEO_FLAG_PARAM_SETS_PRECEDE
    assert not (flags & VIDEO_FLAG_DISCARDABLE)

    au_p = SC4 + aud + SC3 + nonidr
    p_flags = video_frame_flags(split_nals(au_p))
    assert not (p_flags & VIDEO_FLAG_IDR)
    assert not (p_flags & VIDEO_FLAG_PARAM_SETS_PRECEDE)

    # avcc_from_au_for_decode: AUD + in-band SPS/PPS are stripped; the IDR slice
    # (and SEI) are kept. The flags above still detect IDR/paramSetsPrecede from
    # the FULL NAL list, so the AVCC-for-decode stripping must not affect them.
    sei = bytes([0x06, 0x05, 0x01, 0x80])  # type 6 SEI
    au_full = SC4 + aud + SC3 + sps + SC4 + pps + SC4 + sei + SC4 + idr
    decode_avcc = avcc_from_au_for_decode(au_full)
    decode_nals = avcc_to_nals(decode_avcc)
    decode_types = [nal_type(n) for n in decode_nals]
    assert NAL_AUD not in decode_types, decode_types        # AUD stripped
    assert NAL_SPS not in decode_types, decode_types        # SPS sent separately
    assert NAL_PPS not in decode_types, decode_types        # PPS sent separately
    assert NAL_SLICE_IDR in decode_types, decode_types      # IDR slice kept
    assert NAL_SEI in decode_types, decode_types            # SEI kept
    assert decode_types == [NAL_SEI, NAL_SLICE_IDR], decode_types
    # Flag detection over the full AU is unchanged by the stripping.
    full_flags = video_frame_flags(split_nals(au_full))
    assert full_flags & VIDEO_FLAG_IDR
    assert full_flags & VIDEO_FLAG_PARAM_SETS_PRECEDE
    # A non-IDR AU keeps its slice and still drops the AUD.
    p_decode_types = [nal_type(n) for n in
                      avcc_to_nals(avcc_from_au_for_decode(au_p))]
    assert p_decode_types == [NAL_SLICE_NON_IDR], p_decode_types

    # Access-unit splitter on a 2-AU stream (each begins with an AUD).
    two_aus = (SC4 + aud + SC3 + sps + SC4 + pps + SC4 + idr) + (SC4 + aud + SC3 + nonidr)
    aus = split_access_units(two_aus)
    assert len(aus) == 2, len(aus)
    assert split_nals(aus[0]) == [aud, sps, pps, idr]
    assert split_nals(aus[1]) == [aud, nonidr]
    # Each AU converts cleanly to AVCC and back.
    for au in aus:
        assert avcc_to_nals(annexb_to_avcc(au)) == split_nals(au)

    print("protocol.py self-test OK: header=24B hello=16B device_info=16B "
          "stream_config=12B(prim=1) ack=2B; VIDEO_PARAM_SETS/VIDEO_FRAME "
          "pack/unpack + annexb->avcc + AU split all pass.")
