"""UK phone number normalising and classification."""
from __future__ import annotations

import re
from dataclasses import dataclass

MOBILE = "mobile"
LANDLINE = "landline"
NON_GEOGRAPHIC = "non_geographic"
INVALID = "invalid"
NONE = "none"


@dataclass(frozen=True)
class PhoneInfo:
    raw: str
    national: str  # e.g. 07700900123 ('' when invalid)
    e164: str  # e.g. +447700900123 ('' when invalid)
    kind: str  # mobile | landline | non_geographic | invalid | none

    @property
    def valid(self) -> bool:
        return self.kind in {MOBILE, LANDLINE, NON_GEOGRAPHIC}


def to_national_digits(raw: str | None) -> str:
    """Reduce any UK format ('+44 (0)1293 123456', '0044 1293...', '01293-123456') to 0XXXXXXXXXX.
    Foreign numbers come back as '+<digits>'."""
    if not raw:
        return ""
    s = re.split(r"(?i)\s*(?:ext\.?|extension|x)\s*\d+\s*$", raw.strip())[0]
    s = s.replace("(0)", "")
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if plus:
        return "0" + digits[2:] if digits.startswith("44") else "+" + digits
    if digits.startswith("0044"):
        return "0" + digits[4:]
    if digits.startswith("44") and len(digits) in (11, 12):
        return "0" + digits[2:]
    return digits


def classify(raw: str | None) -> PhoneInfo:
    raw = (raw or "").strip()
    if not raw:
        return PhoneInfo(raw, "", "", NONE)
    nat = to_national_digits(raw)
    if not nat.startswith("0") or nat.startswith("00"):
        return PhoneInfo(raw, "", "", INVALID)
    n = len(nat)
    kind = INVALID
    if nat.startswith("07"):
        if n == 11:
            if nat.startswith("07624") or nat[2] not in "06":
                kind = MOBILE
            else:  # 070 personal numbers, 076 pagers
                kind = NON_GEOGRAPHIC
    elif nat.startswith(("01", "02")):
        if n in (10, 11):
            kind = LANDLINE
    elif nat.startswith(("03", "05", "08", "09")):
        if n in (10, 11):
            kind = NON_GEOGRAPHIC
    if kind == INVALID:
        return PhoneInfo(raw, "", "", INVALID)
    return PhoneInfo(raw, nat, "+44" + nat[1:], kind)


def normalise_e164(raw: str | None) -> str:
    """E.164 form for matching ('' if not a valid UK number). Foreign numbers keep their digits."""
    info = classify(raw)
    if info.valid:
        return info.e164
    nat = to_national_digits(raw)
    if nat.startswith("+") and len(nat) > 8:
        return nat
    return ""


def pretty(raw: str | None) -> str:
    """Human-friendly UK formatting for display."""
    info = classify(raw)
    if not info.valid:
        return raw or ""
    n = info.national
    if n.startswith("02") and len(n) == 11:
        return f"{n[:3]} {n[3:7]} {n[7:]}"
    if n.startswith(("03", "08", "09")) and len(n) == 11:
        return f"{n[:4]} {n[4:7]} {n[7:]}"
    if len(n) == 11:
        return f"{n[:5]} {n[5:]}"
    return f"{n[:4]} {n[4:]}"
