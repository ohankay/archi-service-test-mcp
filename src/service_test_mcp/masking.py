"""Masking of sensitive data in everything that is returned to the model or written to the audit log.

Assertions and ``extract`` always work on the real values; only the *view* is masked.

Built-in rules (``masking.builtin: true``):

* Field names - secrets are fully hidden (password, sifre, token, secret, cvv, ...); identity / account / card / phone
  fields keep only their last ``keep_last`` characters.
* Values anywhere in the response - Turkish ID numbers (TCKN, checksum validated), TR IBANs, card numbers (Luhn
  validated), Turkish mobile numbers and e-mail addresses.
* Exact secret values (passwords, tokens read from the environment) are replaced wherever they appear.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any, Iterable

from .config import MaskingConfig

SECRET_FIELDS = ("*password*", "*passwd*", "*sifre*", "*şifre*", "*parola*", "*secret*", "*token*", "authorization",
                 "*apikey*", "*api_key*", "*api-key*", "cvv*", "cvc*", "*pin", "pin*", "*otp*", "*cookie*",
                 "*sessionkey*", "*session_key*")
PARTIAL_FIELDS = ("*tckn*", "*tcno*", "*tc_no*", "*tckimlik*", "*kimlikno*", "*kimlik_no*", "*identitynumber*",
                  "*nationalid*", "*iban*", "*kartno*", "*kart_no*", "*cardnumber*", "*card_number*", "pan",
                  "*telefon*", "*phone*", "*gsm*", "ceptel*", "*cep_tel*", "cepno*", "*email*", "*eposta*")

_TCKN = re.compile(r"(?<!\d)[1-9]\d{10}(?!\d)")
_IBAN = re.compile(r"\bTR\d{2}(?:[ ]?\d{4}){5}[ ]?\d{2}\b", re.IGNORECASE)
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_PHONE = re.compile(r"(?<!\d)(?:\+90|0090|0)?[ ]?5\d{2}[ ]?\d{3}[ ]?\d{2}[ ]?\d{2}(?!\d)")
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")


def valid_tckn(digits: str) -> bool:
    if len(digits) != 11 or not digits.isdigit() or digits[0] == "0":
        return False
    d = [int(c) for c in digits]
    d10 = ((d[0] + d[2] + d[4] + d[6] + d[8]) * 7 - (d[1] + d[3] + d[5] + d[7])) % 10
    return d10 == d[9] and sum(d[:10]) % 10 == d[10]


def luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


class Masker:
    def __init__(self, config: MaskingConfig | None = None, secrets: Iterable[str] = ()) -> None:
        self.config = config or MaskingConfig()
        self.secrets = {s for s in secrets if s and len(s) >= 4}
        self._extra = [(name, re.compile(rx)) for name, rx in self.config.patterns.items()]

    def add_secret(self, value: str | None) -> None:
        if value and len(value) >= 4:
            self.secrets.add(value)

    # -- helpers -------------------------------------------------------------------------------------------------
    def _partial(self, value: str) -> str:
        keep = self.config.keep_last
        if len(value) <= keep:
            return "*" * len(value)
        return "*" * (len(value) - keep) + value[-keep:] if keep else "***"

    def _field_kind(self, key: str) -> str | None:
        key = key.lower()
        if any(fnmatch.fnmatch(key, p.lower()) for p in self.config.unmask_fields):
            return "plain"
        if self.config.builtin:
            if any(fnmatch.fnmatch(key, p) for p in SECRET_FIELDS):
                return "secret"
            if any(fnmatch.fnmatch(key, p) for p in PARTIAL_FIELDS):
                return "partial"
        if any(fnmatch.fnmatch(key, p.lower()) for p in self.config.fields):
            return "partial"
        return None

    def mask_text(self, text: str) -> str:
        if not self.config.enabled or not text:
            return text
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "***")
        if self.config.builtin:
            text = _IBAN.sub(lambda m: self._partial(m.group(0).replace(" ", "")), text)
            text = _TCKN.sub(lambda m: self._partial(m.group(0)) if valid_tckn(m.group(0)) else m.group(0), text)

            def card(m: re.Match) -> str:
                digits = re.sub(r"[ -]", "", m.group(0))
                return self._partial(digits) if 13 <= len(digits) <= 19 and luhn(digits) else m.group(0)

            text = _CARD.sub(card, text)
            text = _PHONE.sub(lambda m: self._partial(re.sub(r"\D", "", m.group(0))), text)
            text = _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)
        for _, rx in self._extra:
            text = rx.sub(lambda m: self._partial(m.group(0)), text)
        return text

    def mask(self, obj: Any, _key: str | None = None) -> Any:
        """Return a masked deep copy (dict / list / scalars)."""
        if not self.config.enabled:
            return obj
        kind = self._field_kind(_key) if _key else None
        if isinstance(obj, dict):
            return {k: self.mask(v, str(k)) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.mask(v, _key) for v in obj]
        if obj is None or isinstance(obj, bool):
            return obj
        if kind == "secret":
            return "***"
        if kind == "partial":
            return self._partial(str(obj))
        if kind == "plain":
            return obj
        if isinstance(obj, int) and len(str(abs(obj))) >= 11:
            masked = self.mask_text(str(obj))
            return masked if masked != str(obj) else obj
        if isinstance(obj, str):
            return self.mask_text(obj)
        return obj

    def mask_headers(self, headers: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for k, v in headers.items():
            low = k.lower()
            if low in ("authorization", "proxy-authorization", "cookie", "set-cookie", "www-authenticate") or \
                    self._field_kind(low) == "secret":
                out[k] = "***"
            else:
                out[k] = self.mask_text(str(v))
        return out
