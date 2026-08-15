#!/usr/bin/env python3
"""Extract plain text from a PDF using only the Python standard library.

Usage:
    python extract_pdf_text.py input.pdf -o output.txt
    python extract_pdf_text.py input.pdf            # prints to stdout

Exit codes:
    0  success
    1  file / usage error
    2  extraction produced no usable text (scanned or encrypted PDF)

The extractor handles FlateDecode-compressed streams, literal and hex
strings, Tj/TJ text operators, and ToUnicode CMaps (bfchar / bfrange)
for CID-encoded Chinese text. It is a fallback tool: for complex
layouts, prefer pasting the document text directly.
"""

from __future__ import annotations

import argparse
import re
import sys
import zlib
from pathlib import Path

STREAM_RE = re.compile(rb"stream\r?\n")
MIN_CHARS = 50


def find_streams(data: bytes):
    """Yield (dictionary_bytes, decoded_stream_bytes) for each stream object."""
    for match in STREAM_RE.finditer(data):
        body_start = match.end()
        body_end = data.find(b"endstream", body_start)
        if body_end < 0:
            continue
        raw = data[body_start:body_end]
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        elif raw.endswith(b"\n"):
            raw = raw[:-1]
        dict_start = data.rfind(b"<<", 0, match.start())
        dict_bytes = data[dict_start:match.start()] if dict_start >= 0 else b""
        if b"FlateDecode" in dict_bytes:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                continue
        yield dict_bytes, raw


def _parse_dst(hex_text: str) -> str:
    """Convert a UTF-16BE hex string to text, tolerating odd input."""
    if len(hex_text) % 4 != 0:
        hex_text = hex_text[: len(hex_text) - (len(hex_text) % 4)]
    try:
        return bytes.fromhex(hex_text).decode("utf-16-be", errors="replace")
    except ValueError:
        return ""


def parse_tounicode(stream: bytes) -> dict[str, str]:
    """Parse bfchar / bfrange entries from a ToUnicode CMap stream."""
    cmap: dict[str, str] = {}
    text = stream.decode("latin-1", errors="replace")

    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.DOTALL):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            cmap[src.upper()] = _parse_dst(dst)

    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.DOTALL):
        for lo, hi, dst in re.findall(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block
        ):
            base = int(dst, 16)
            for offset, code in enumerate(range(int(lo, 16), int(hi, 16) + 1)):
                cmap[f"{code:0{len(lo)}X}"] = chr(base + offset)
        for lo, hi, array in re.findall(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", block, re.DOTALL
        ):
            items = re.findall(r"<([0-9A-Fa-f]+)>", array)
            for offset, dst in enumerate(items):
                cmap[f"{int(lo, 16) + offset:0{len(lo)}X}"] = _parse_dst(dst)
    return cmap


def decode_literal(raw: bytes) -> str:
    """Decode a PDF literal string body (escapes already processed upstream)."""
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace")
    return raw.decode("latin-1", errors="replace")


def unescape_literal(body: bytes) -> bytes:
    """Resolve backslash escapes inside a literal string."""
    out = bytearray()
    i = 0
    escapes = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
    while i < len(body):
        ch = body[i]
        if ch == 0x5C and i + 1 < len(body):  # backslash
            nxt = body[i + 1]
            if nxt in escapes:
                out.append(escapes[nxt])
                i += 2
            elif nxt in (ord("("), ord(")"), 0x5C):
                out.append(nxt)
                i += 2
            elif 48 <= nxt <= 55:  # octal, up to 3 digits
                digits = bytes([nxt])
                j = i + 2
                while j < len(body) and len(digits) < 3 and 48 <= body[j] <= 55:
                    digits += bytes([body[j]])
                    j += 1
                out.append(int(digits, 8) & 0xFF)
                i = j
            elif nxt in (10, 13):  # line continuation
                i += 2
                if nxt == 13 and i < len(body) and body[i] == 10:
                    i += 1
            else:
                out.append(nxt)
                i += 2
        else:
            out.append(ch)
            i += 1
    return bytes(out)


def hex_to_text(hex_str: str, cmap: dict[str, str], code_len: int) -> str:
    """Map a hex string through the CMap, falling back to latin-1."""
    hex_str = re.sub(r"\s+", "", hex_str)
    if not hex_str:
        return ""
    if cmap and len(hex_str) % code_len == 0:
        chars = []
        for i in range(0, len(hex_str), code_len):
            code = hex_str[i : i + code_len].upper()
            chars.append(cmap.get(code, ""))
        text = "".join(chars)
        if text:
            return text
    try:
        raw = bytes.fromhex(hex_str)
    except ValueError:
        return ""
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace")
    return raw.decode("latin-1", errors="replace")


def literal_to_text(body: bytes, cmap: dict[str, str], code_len: int) -> str:
    """Map a literal string through the CMap when it looks CID-encoded."""
    raw = unescape_literal(body)
    if cmap and raw and len(raw) % (code_len // 2) == 0:
        hex_str = raw.hex().upper()
        chars = []
        for i in range(0, len(hex_str), code_len):
            code = hex_str[i : i + code_len]
            chars.append(cmap.get(code, ""))
        text = "".join(chars)
        if text.strip():
            return text
    return decode_literal(raw)


def extract_from_content(stream: bytes, cmap: dict[str, str], code_len: int) -> str:
    """Extract text-show operations from a content stream."""
    pieces: list[str] = []
    i = 0
    n = len(stream)

    while i < n:
        ch = stream[i : i + 1]
        if ch == b"(":  # literal string
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if stream[j : j + 1] == b"\\":
                    j += 2
                    continue
                if stream[j : j + 1] == b"(":
                    depth += 1
                elif stream[j : j + 1] == b")":
                    depth -= 1
                j += 1
            literal = literal_to_text(stream[i + 1 : j - 1], cmap, code_len)
            tail = stream[j : j + 20]
            if re.match(rb"\s*(Tj|'|\")", tail):
                pieces.append(literal)
            else:
                pieces.append(literal)  # inside TJ array
            i = j
        elif ch == b"<" and stream[i : i + 2] != b"<<":  # hex string
            j = stream.find(b">", i + 1)
            if j < 0:
                break
            pieces.append(hex_to_text(stream[i + 1 : j].decode("latin-1"), cmap, code_len))
            i = j + 1
        elif ch == b"]":  # end of TJ array: treat as space boundary
            pieces.append(" ")
            i += 1
        elif stream[i : i + 2] in (b"Td", b"TD", b"T*") or stream[i : i + 1] == b"'":
            pieces.append("\n")
            i += 2
        else:
            i += 1
    return "".join(pieces)


def extract_text(pdf_path: Path) -> str:
    data = pdf_path.read_bytes()
    if not data.startswith(b"%PDF"):
        raise ValueError("不是有效的 PDF 文件（缺少 %PDF 头）")

    cmaps: list[dict[str, str]] = []
    contents: list[bytes] = []
    for dict_bytes, stream in find_streams(data):
        if b"beginbfchar" in stream or b"beginbfrange" in stream:
            cmap = parse_tounicode(stream)
            if cmap:
                cmaps.append(cmap)
        elif b"BT" in stream:
            contents.append(stream)

    merged: dict[str, str] = {}
    for cmap in cmaps:
        merged.update(cmap)
    code_len = 4
    if merged:
        code_len = max(len(code) for code in merged)
        if code_len % 2 != 0:
            code_len += 1

    parts = [extract_from_content(stream, merged, code_len) for stream in contents]
    text = "\n".join(part for part in parts if part.strip())
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="从 PDF 提取纯文字（仅标准库）")
    parser.add_argument("pdf", type=Path, help="输入 PDF 路径")
    parser.add_argument("-o", "--output", type=Path, help="输出文本路径；缺省打印到屏幕")
    args = parser.parse_args()

    if not args.pdf.is_file():
        print(f"错误：文件不存在：{args.pdf}", file=sys.stderr)
        return 1
    try:
        text = extract_text(args.pdf)
    except (OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if len(text) < MIN_CHARS:
        print(
            "提取失败：几乎没有读到文字。这份 PDF 可能是扫描件或已加密，"
            "请改用文字版 PDF，或直接把内容粘贴给助手。",
            file=sys.stderr,
        )
        return 2

    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"已提取 {len(text)} 字 → {args.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
