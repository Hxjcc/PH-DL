"""Small synthetic files with MP4 box boundaries; no external media is needed."""


def mp4_payload(sample: bytes = b"\xab") -> bytes:
    def box(kind: bytes, data: bytes) -> bytes:
        return (len(data) + 8).to_bytes(4, "big") + kind + data

    return box(b"ftyp", b"isom\x00\x00\x02\x00isom") + box(b"moov", b"\x00" * 12) + box(b"mdat", sample * 256)
