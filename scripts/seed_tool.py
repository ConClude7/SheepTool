#!/usr/bin/env python3
"""Decode/replay Sheep seed packets used by map_info_ex_seed.

The official client sends:

    byte 0      fixed 1
    byte 1..2   uint16 opcode, big endian (30143 for C2SS_GetSeed)
    byte 3..    protobuf payload, AES-OFB encrypted when need_wx_encrypt=true

This helper keeps the seed reverse-engineering path repeatable.  It can derive
the OFB keystream from a captured request with known map_seed_2, decrypt a saved
response with either that keystream or the full wx encryptKey/iv, and parse the
SS2C_GetSeedAck protobuf fields.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


OPCODE_GET_SEED = 30143


def _b64decode(text: str) -> bytes:
    text = text.strip()
    padding = "=" * (-len(text) % 4)
    return base64.b64decode(text.replace("-", "+").replace("_", "/") + padding)


def _b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _read_bytes(path_or_hex: str | None, *, default: bytes = b"") -> bytes:
    if not path_or_hex:
        return default
    path = Path(path_or_hex)
    if path.exists():
        return path.read_bytes()
    return bytes.fromhex(path_or_hex.replace(" ", ""))


def _read_ciphertext(value: str, *, fmt: str) -> bytes:
    path = Path(value)
    if path.exists():
        return path.read_bytes()
    if fmt == "hex":
        return bytes.fromhex(value.replace(" ", ""))
    if fmt == "base64":
        return _b64decode(value)
    raise ValueError(f"unknown ciphertext format: {fmt}")


def _read_text_or_value(value: str | None) -> str | None:
    if not value:
        return None
    path = Path(value)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return value


def _read_plaintext(value: str | None, *, fmt: str) -> bytes:
    value = _read_text_or_value(value) or ""
    if fmt == "utf8":
        return value.encode("utf-8")
    if fmt == "uri":
        return urllib.parse.quote(value, safe="-_.!~*'()").encode("ascii")
    if fmt == "hex":
        return bytes.fromhex(value.replace(" ", ""))
    if fmt == "base64":
        return _b64decode(value)
    raise ValueError(f"unknown plaintext format: {fmt}")


def _decode_key(value: str, fmt: str) -> bytes:
    value = _read_text_or_value(value) or ""
    if fmt == "utf8":
        return value.encode("utf-8")
    if fmt == "hex":
        return bytes.fromhex(value.replace(" ", ""))
    if fmt == "base64":
        return _b64decode(value)
    raise ValueError(f"unknown key format: {fmt}")


def _xor(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right))


def encode_varint(value: int) -> bytes:
    out = bytearray()
    value = int(value)
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    start = pos
    while pos < len(data):
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise ValueError(f"varint too long at byte {start}")
    raise ValueError(f"truncated varint at byte {start}")


def encode_get_seed_plain(seed: str) -> bytes:
    payload = b"\x0a" + encode_varint(len(seed.encode("utf-8"))) + seed.encode("utf-8")
    return bytes([1, OPCODE_GET_SEED >> 8, OPCODE_GET_SEED & 0xFF]) + payload


def derive_keystream_from_request(info_b64: str, seed: str) -> bytes:
    encrypted = _b64decode(info_b64)
    plain = encode_get_seed_plain(seed)
    if len(encrypted) != len(plain):
        raise ValueError(
            f"captured request length mismatch: encrypted={len(encrypted)}, plain={len(plain)}"
        )
    if encrypted[:3] != plain[:3]:
        raise ValueError(
            "request header mismatch; expected an unencrypted [1, 0x75, 0xbf] prefix"
        )
    return _xor(encrypted[3:], plain[3:])


def aes_ofb(data: bytes, key: bytes, iv: bytes) -> bytes:
    try:
        from Crypto.Cipher import AES
    except ImportError as exc:
        raise RuntimeError(
            "AES 解密需要 pycryptodome：请先运行 `pip install pycryptodome`"
        ) from exc
    if len(key) not in {16, 24, 32}:
        raise ValueError(f"AES key length must be 16/24/32 bytes, got {len(key)}")
    if len(iv) != 16:
        raise ValueError(f"AES OFB iv length must be 16 bytes, got {len(iv)}")
    return AES.new(key, AES.MODE_OFB, iv=iv).encrypt(data)


@dataclass
class SeedAck:
    code: int | None
    map_seed: list[int]
    map_seed_2: str | None


def decode_seed_ack(data: bytes) -> SeedAck:
    pos = 0
    code: int | None = None
    map_seed: list[int] = []
    map_seed_2: str | None = None

    while pos < len(data):
        tag, pos = decode_varint(data, pos)
        field = tag >> 3
        wire = tag & 7

        if field == 1 and wire == 0:
            code, pos = decode_varint(data, pos)
        elif field == 2:
            if wire == 2:
                length, pos = decode_varint(data, pos)
                end = pos + length
                if end > len(data):
                    raise ValueError("truncated packed mapSeed field")
                while pos < end:
                    value, pos = decode_varint(data, pos)
                    map_seed.append(value)
            elif wire == 0:
                value, pos = decode_varint(data, pos)
                map_seed.append(value)
            else:
                raise ValueError(f"unsupported mapSeed wire type: {wire}")
        elif field == 3 and wire == 2:
            length, pos = decode_varint(data, pos)
            raw = data[pos : pos + length]
            if len(raw) != length:
                raise ValueError("truncated mapSeed2 field")
            map_seed_2 = raw.decode("utf-8", errors="replace")
            pos += length
        else:
            pos = skip_unknown(data, pos, wire)

    return SeedAck(code=code, map_seed=map_seed, map_seed_2=map_seed_2)


def skip_unknown(data: bytes, pos: int, wire: int) -> int:
    if wire == 0:
        _, pos = decode_varint(data, pos)
        return pos
    if wire == 1:
        return pos + 8
    if wire == 2:
        length, pos = decode_varint(data, pos)
        return pos + length
    if wire == 5:
        return pos + 4
    raise ValueError(f"unsupported protobuf wire type: {wire}")


def decrypt_response(args: argparse.Namespace) -> bytes:
    response = _read_bytes(args.response)
    if args.key and args.iv:
        key = _decode_key(args.key, args.key_format)
        iv = _decode_key(args.iv, args.iv_format)
        return aes_ofb(response, key, iv)

    keystream = _read_bytes(args.keystream_hex)
    if args.request_info and args.seed:
        derived = derive_keystream_from_request(_read_text_or_value(args.request_info) or "", args.seed)
        keystream = keystream + derived if keystream else derived

    if not keystream:
        raise ValueError("need --key/--iv, --keystream-hex, or --request-info with --seed")
    if len(keystream) < len(response):
        print(
            f"警告：keystream 只有 {len(keystream)} 字节，响应有 {len(response)} 字节；"
            "只能部分解密。",
            file=sys.stderr,
        )
    return _xor(response, keystream)


def cmd_decode(args: argparse.Namespace) -> None:
    plain = decrypt_response(args)
    print(f"plaintext_hex={plain.hex(' ')}")
    try:
        ack = decode_seed_ack(plain)
    except ValueError as exc:
        print(f"protobuf_parse_error={exc}", file=sys.stderr)
        partial = decode_seed_ack_partial(plain)
        if partial:
            print(json.dumps(partial, ensure_ascii=False, indent=2))
        return

    result = {
        "code": ack.code,
        "map_seed": ack.map_seed,
        "map_seed_2": ack.map_seed_2,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_derive(args: argparse.Namespace) -> None:
    cipher = _read_ciphertext(args.ciphertext, fmt=args.ciphertext_format)
    plain = _read_plaintext(args.plaintext, fmt=args.plaintext_format)
    if len(plain) > len(cipher):
        raise ValueError(f"plaintext longer than ciphertext: {len(plain)} > {len(cipher)}")
    keystream = _xor(cipher, plain)
    if args.output:
        Path(args.output).write_bytes(keystream)
    print(f"keystream_len={len(keystream)}")
    print(f"keystream_hex={keystream.hex(' ')}")


def decode_seed_ack_partial(data: bytes) -> dict:
    """Best-effort parser for truncated known-plaintext OFB output."""
    result: dict[str, object] = {}
    try:
        pos = 0
        tag, pos = decode_varint(data, pos)
        if tag == 0x08:
            result["code"], pos = decode_varint(data, pos)
        tag, pos = decode_varint(data, pos)
        if tag == 0x12:
            packed_len, pos = decode_varint(data, pos)
            result["map_seed_packed_len"] = packed_len
            values = []
            while pos < len(data):
                try:
                    value, pos = decode_varint(data, pos)
                except ValueError:
                    result["truncated_at_byte"] = pos
                    break
                values.append(value)
            result["map_seed_partial"] = values
    except ValueError:
        pass
    return result


def cmd_request(args: argparse.Namespace) -> None:
    key = _decode_key(args.key, args.key_format)
    iv = _decode_key(args.iv, args.iv_format)
    plain = encode_get_seed_plain(args.seed)
    encrypted = bytearray(plain)
    encrypted[3:] = aes_ofb(plain[3:], key, iv)

    body = {
        "encryptKeyVersion": args.encrypt_key_version,
        "info": _b64encode(bytes(encrypted)),
    }
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        args.url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "b": str(args.build),
            "t": args.token,
            "Referer": args.referer,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            encrypted_response = resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}") from exc

    plain_response = aes_ofb(encrypted_response, key, iv)
    ack = decode_seed_ack(plain_response)
    if args.output:
        Path(args.output).write_bytes(encrypted_response)
    print(json.dumps({
        "response_len": len(encrypted_response),
        "response_hex": encrypted_response.hex(" "),
        "code": ack.code,
        "map_seed": ack.map_seed,
        "map_seed_2": ack.map_seed_2,
    }, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="羊了个羊 seed 接口诊断/解密工具")
    sub = parser.add_subparsers(dest="command", required=True)

    dec = sub.add_parser("decode", help="解密并解析保存的 seed 响应")
    dec.add_argument("--response", required=True, help="响应二进制文件路径，或 hex 字符串")
    dec.add_argument("--request-info", help="请求 JSON 里的 info，或保存 info 的文本文件")
    dec.add_argument("--seed", help="本次 map_seed_2，用于从已知请求明文推导 keystream")
    dec.add_argument("--keystream-hex", help="额外/直接提供 OFB keystream hex，或文件路径")
    dec.add_argument("--key", help="wx.getLatestUserKey 返回的 encryptKey")
    dec.add_argument("--iv", help="wx.getLatestUserKey 返回的 iv")
    dec.add_argument("--key-format", choices=["utf8", "hex", "base64"], default="utf8")
    dec.add_argument("--iv-format", choices=["utf8", "hex", "base64"], default="utf8")
    dec.set_defaults(func=cmd_decode)

    drv = sub.add_parser("derive", help="从已知明文/密文推出 OFB keystream")
    drv.add_argument("--ciphertext", required=True, help="密文文件路径、hex 字符串或 base64 字符串")
    drv.add_argument(
        "--ciphertext-format",
        choices=["hex", "base64"],
        default="hex",
        help="当 --ciphertext 不是文件路径时的编码格式",
    )
    drv.add_argument("--plaintext", required=True, help="明文字符串/文件/hex/base64")
    drv.add_argument(
        "--plaintext-format",
        choices=["utf8", "uri", "hex", "base64"],
        default="utf8",
        help="结算上传的 textToUint8Array(o) 对应 uri，即 encodeURIComponent(JSON.stringify(o))",
    )
    drv.add_argument("--output", help="保存 keystream bytes 到文件")
    drv.set_defaults(func=cmd_derive)

    req = sub.add_parser("request", help="用 wx encryptKey/iv 复放 seed 请求并解析响应")
    req.add_argument("--seed", required=True, help="map_seed_2")
    req.add_argument("--key", required=True, help="wx.getLatestUserKey 返回的 encryptKey")
    req.add_argument("--iv", required=True, help="wx.getLatestUserKey 返回的 iv")
    req.add_argument("--token", required=True, help="请求头 t")
    req.add_argument("--build", default="1120", help="请求头 b，默认 1120")
    req.add_argument("--encrypt-key-version", type=int, required=True)
    req.add_argument(
        "--url",
        default="https://cat-match.easygame2021.com/sheep/v1/game/map_info_ex_seed?isByte=true",
    )
    req.add_argument(
        "--referer",
        default="https://servicewechat.com/wx141bfb9b73c970a9/462/page-frame.html",
    )
    req.add_argument("--key-format", choices=["utf8", "hex", "base64"], default="utf8")
    req.add_argument("--iv-format", choices=["utf8", "hex", "base64"], default="utf8")
    req.add_argument("--timeout", type=float, default=30)
    req.add_argument("--output", help="保存加密响应体到文件")
    req.set_defaults(func=cmd_request)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
