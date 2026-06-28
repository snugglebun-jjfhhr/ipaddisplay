/*
 * ipaddisplay wire protocol — canonical C header.
 *
 * Source of truth: docs/protocol.md. Mirror of host/protocol.py.
 *
 * Wire format is LITTLE-ENDIAN, packed. Both ends (x86-64 host, ARM64 iPad)
 * are little-endian, so structs are memcpy-compatible with the wire with zero
 * byteswaps. The #pragma pack below removes implicit padding so sizeof matches
 * the on-wire layout exactly.
 *
 * This header declares the full M0-M2 subset (header + handshake/keepalive).
 * Video/still message types are enumerated but their payload structs are left
 * for M3+.
 */

#ifndef IPADDISPLAY_PROTOCOL_H
#define IPADDISPLAY_PROTOCOL_H

#include <stdint.h>

#if defined(__cplusplus)
#include <cassert>
#define IPDP_STATIC_ASSERT(cond, msg) static_assert(cond, msg)
#else
#include <assert.h>
#define IPDP_STATIC_ASSERT(cond, msg) _Static_assert(cond, msg)
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ types */

typedef enum IpdpMessageType {
    IPDP_HELLO              = 0x01, /* H->D */
    IPDP_DEVICE_INFO       = 0x02, /* D->H */
    IPDP_STREAM_CONFIG     = 0x03, /* H->D */
    IPDP_STREAM_CONFIG_ACK = 0x04, /* D->H */
    IPDP_TEARDOWN          = 0x05, /* both */
    IPDP_PING              = 0x06, /* both */
    IPDP_PONG              = 0x07, /* both */
    IPDP_FRAME_PRESENTED   = 0x08, /* D->H */
    IPDP_ERROR             = 0x09, /* both */
    IPDP_VIDEO_PARAM_SETS  = 0x10, /* H->D */
    IPDP_VIDEO_FRAME       = 0x11, /* H->D */
    IPDP_SET_LAYER         = 0x20, /* H->D */
    IPDP_STILL_BEGIN       = 0x21, /* H->D */
    IPDP_STILL_TILE        = 0x22, /* H->D */
    IPDP_STILL_COMMIT      = 0x23  /* H->D */
} IpdpMessageType;

/* codecMask / decoderMask bits */
enum {
    IPDP_CODEC_H264 = 1u << 0,
    IPDP_CODEC_HEVC = 1u << 1
};

#define IPDP_HELLO_MAGIC   "IPDP"  /* 4 bytes, NOT null-terminated on the wire */
#define IPDP_PROTO_VERSION 1

/* --------------------------------------------------------------- structs */

#pragma pack(push, 1)

/* 24 bytes. Prefixes every message; `length` payload bytes follow. */
typedef struct IpdpMsgHeader {
    uint8_t  type;        /* offset 0  : IpdpMessageType */
    uint8_t  flags;       /* offset 1  : type-specific bitfield */
    uint16_t _pad;        /* offset 2  : 0 */
    uint32_t seq;         /* offset 4  : monotonic per-message sequence */
    uint32_t length;      /* offset 8  : payload bytes that follow */
    uint32_t _pad2;       /* offset 12 : 0 (keeps timestampUs 8-byte aligned) */
    uint64_t timestampUs; /* offset 16 : host monotonic clock, microseconds */
} IpdpMsgHeader;
IPDP_STATIC_ASSERT(sizeof(IpdpMsgHeader) == 24, "MsgHeader must be 24 bytes");

/* HELLO (H->D), 16 bytes. */
typedef struct IpdpHello {
    char     magic[4];       /* offset 0  : 'IPDP' */
    uint16_t protoVer;       /* offset 4  : = 1 */
    uint16_t hostFlags;      /* offset 6  */
    uint32_t codecMask;      /* offset 8  : bit0=H264, bit1=HEVC */
    uint32_t maxBitrateKbps; /* offset 12 */
} IpdpHello;
IPDP_STATIC_ASSERT(sizeof(IpdpHello) == 16, "HELLO must be 16 bytes");

/* DEVICE_INFO (D->H), 16 bytes. */
typedef struct IpdpDeviceInfo {
    uint16_t nativeW;      /* offset 0  : = 2732 */
    uint16_t nativeH;      /* offset 2  : = 2048 */
    uint16_t maxDecodeW;   /* offset 4  */
    uint16_t maxDecodeH;   /* offset 6  */
    uint32_t decoderMask;  /* offset 8  : bit0=H264, bit1=HEVC */
    uint8_t  maxRefreshHz; /* offset 12 */
    uint8_t  supportsP3;   /* offset 13 */
    uint8_t  hwHEVC;       /* offset 14 */
    uint8_t  _pad;         /* offset 15 : 0 */
} IpdpDeviceInfo;
IPDP_STATIC_ASSERT(sizeof(IpdpDeviceInfo) == 16, "DEVICE_INFO must be 16 bytes");

/* STREAM_CONFIG (H->D), 12 bytes. */
typedef struct IpdpStreamConfig {
    uint16_t w;              /* offset 0  */
    uint16_t h;              /* offset 2  */
    uint8_t  fpsCap;         /* offset 4  */
    uint8_t  codec;          /* offset 5  : 0=H264 */
    uint8_t  chroma;         /* offset 6  : 0=420 */
    uint8_t  bitDepth;       /* offset 7  : 8 */
    uint8_t  fullRange;      /* offset 8  : 1 */
    uint8_t  colorPrimaries; /* offset 9  : 1=BT.709 (RECONCILED from 12; stream is 709/sRGB) */
    uint8_t  transfer;       /* offset 10 : 13=sRGB */
    uint8_t  matrix;         /* offset 11 : 1=BT.709 */
} IpdpStreamConfig;
IPDP_STATIC_ASSERT(sizeof(IpdpStreamConfig) == 12, "STREAM_CONFIG must be 12 bytes");

/* STREAM_CONFIG_ACK (D->H), 2 bytes. */
typedef struct IpdpStreamConfigAck {
    uint8_t ok;     /* offset 0 */
    uint8_t reason; /* offset 1 */
} IpdpStreamConfigAck;
IPDP_STATIC_ASSERT(sizeof(IpdpStreamConfigAck) == 2, "STREAM_CONFIG_ACK must be 2 bytes");

#pragma pack(pop)

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* IPADDISPLAY_PROTOCOL_H */
