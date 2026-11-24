"""OpenResty Proof-of-Work (PoW) JS challenge detection and solver."""

from __future__ import annotations

import re
from http.cookies import SimpleCookie
from typing import List, Optional

MAX_CHALLENGE_SOLVES = 6


def is_challenge(html: str) -> bool:
    if not html:
        return False
    return "leastFactor" in html and "document.cookie" in html and len(html) < 8000


def _eval_term(term: str) -> int:
    result = 1
    for part in term.split("*"):
        result *= int(part)
    return result


def _least_factor(n: int) -> int:
    if n <= 1:
        return n
    if n % 2 == 0:
        return 2
    if n % 3 == 0:
        return 3
    if n % 5 == 0:
        return 5
    i = 7
    while i * i <= n:
        for d in (0, 4, 6, 10, 12, 16, 22, 24):
            if n % (i + d) == 0:
                return i + d
        i += 30
    return n


def solve_challenge(html: str) -> str:
    """Solve PoW JS challenge page and return the KEY cookie value."""
    no_comments = re.sub(r"/\*.*?\*/", "", html, flags=re.S)
    compact = re.sub(r"\s+", "", no_comments)

    m_p = re.search(r"varp=(\d+);", compact)
    m_s = re.search(r"vars=(\d+);", compact)
    m_nonce = re.search(r'":"\+s\+":(\d+):1', compact)
    if not (m_p and m_s and m_nonce):
        raise ValueError("PoW challenge parameters missing")

    p = int(m_p.group(1))
    s = int(m_s.group(1))
    nonce = m_nonce.group(1)

    for m in re.finditer(
        r"if\(\(s>>(\d+)\)&1\)p([+-])=([\d*]+);elsep([+-])=([\d*]+);",
        compact,
    ):
        k = int(m.group(1))
        if (s >> k) & 1:
            p = p + _eval_term(m.group(3)) if m.group(2) == "+" else p - _eval_term(m.group(3))
        else:
            p = p + _eval_term(m.group(5)) if m.group(4) == "+" else p - _eval_term(m.group(5))

    m_tail = re.search(r";p([+-])=([\d*]+);n=leastFactor", compact)
    if m_tail:
        p = p + _eval_term(m_tail.group(2)) if m_tail.group(1) == "+" else p - _eval_term(m_tail.group(2))

    n = _least_factor(p)
    if n <= 0 or n * (p // n) != p:
        raise ValueError(f"PoW factorization check failed: {n} * {p // n if n else 0} != {p}")
    return f"{n}*{p // n}:{s}:{nonce}:1"


def extract_set_cookie(set_cookie_headers: List[str], name: str) -> Optional[str]:
    for header in set_cookie_headers:
        cookie = SimpleCookie()
        try:
            cookie.load(header)
        except Exception:
            continue
        if name in cookie:
            return cookie[name].value
    return None
