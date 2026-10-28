"""Masking of sensitive data in everything that is returned to the model or written to the audit log.

Assertions and ``extract`` always work on the real values; only the *view* is masked.

Built-in rules (``masking.builtin: true``):

* Field names - secrets are fully hidden (password, sifre, token, secret, cvv, ...); identity / account / card / phone
  fields keep only their last ``keep_last`` characters.
* Values anywhere in the response - Turkish ID numbers (TCKN, checksum validated), TR IBANs, card numbers (Luhn
  validated), Turkish mobile numbers and e-mail addresses.
* Exact secret values (passwords, tokens read from the environment) are replaced wherever they appear.

Modes (``STM_SEFFAF``):

* ``seffaf`` (default - a learning / test project, nothing is hidden for its own sake): personal data, session keys
  and tokens are shown as they are; only credentials (passwords, OTP - values that came from STM_SECRET_* or the
  local gizli.yaml) are shown by name, e.g. ``«gizli: musteri_sifresi»``. ``STM_GIZLILERI_GOSTER=1`` shows those too.
* ``maskeli``: everything above is masked (production-like data).

The audit log is always written masked (``kati=True``), whatever the mode.
"""

from __future__ import annotations

import fnmatch
import os
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


MODES = ("seffaf", "maskeli")


def mode_from_env() -> str:
    value = (os.environ.get("STM_SEFFAF") or "seffaf").strip().lower()
    return "maskeli" if value in ("maskeli", "0", "false", "hayir", "hayır", "kapali", "kapalı", "masked") \
        else "seffaf"


class Masker:
    def __init__(self, config: MaskingConfig | None = None, secrets: Iterable[str] = (),
                 mode: str | None = None, show_secrets: bool | None = None) -> None:
        self.config = config or MaskingConfig()
        self.secrets: dict[str, str] = {s: "" for s in secrets if s and len(s) >= 4}   # value -> label
        self.credentials: set[str] = set()     # secrets that are credentials (shown by name even in 'seffaf')
        self._extra = [(name, re.compile(rx)) for name, rx in self.config.patterns.items()]
        self.mode = mode or mode_from_env()
        if show_secrets is None:
            show_secrets = os.environ.get("STM_GIZLILERI_GOSTER", "").strip().lower() in ("1", "true", "evet", "yes")
        self.show_secrets = show_secrets

    def add_secret(self, value: str | None, label: str | None = None, credential: bool = False) -> None:
        """Remember a secret value. credential=True: a password / OTP (label = its name, e.g. musteri_sifresi);
        otherwise a session key or token (hidden only in 'maskeli' mode)."""
        if value and len(value) >= 4:
            if label or value not in self.secrets:
                self.secrets[value] = label or self.secrets.get(value, "")
            if credential:
                self.credentials.add(value)

    @property
    def seffaf(self) -> bool:
        return self.mode == "seffaf"

    def _label(self, value: str) -> str:
        label = self.secrets.get(value) or ""
        return f"«gizli: {label}»" if label else "«gizli»"

    def _credentials_only(self, obj: Any) -> Any:
        if self.show_secrets or not self.credentials:
            return obj
        if isinstance(obj, dict):
            return {k: self._credentials_only(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self._credentials_only(v) for v in obj]
        if isinstance(obj, str):
            for secret in sorted(self.credentials, key=len, reverse=True):
                if secret in obj:
                    obj = obj.replace(secret, self._label(secret))
            return obj
        return obj

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

    def mask_text(self, text: str, kati: bool = False) -> str:
        if not text:
            return text
        if self.seffaf and not kati:
            return self._credentials_only(text)
        if not self.config.enabled:
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

    def mask(self, obj: Any, _key: str | None = None, kati: bool = False) -> Any:
        """Return a masked deep copy (dict / list / scalars). kati=True masks fully whatever the mode (audit log)."""
        if self.seffaf and not kati:
            return self._credentials_only(obj)
        if not self.config.enabled:
            return obj
        kind = self._field_kind(_key) if _key else None
        if isinstance(obj, dict):
            return {k: self.mask(v, str(k), kati) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.mask(v, _key, kati) for v in obj]
        if obj is None or isinstance(obj, bool):
            return obj
        if kind == "secret":
            return "***"
        if kind == "partial":
            return self._partial(str(obj))
        if kind == "plain":
            return obj
        if isinstance(obj, int) and len(str(abs(obj))) >= 11:
            masked = self.mask_text(str(obj), kati)
            return masked if masked != str(obj) else obj
        if isinstance(obj, str):
            return self.mask_text(obj, kati)
        return obj

    def mask_headers(self, headers: dict[str, Any], kati: bool = False) -> dict[str, Any]:
        if self.seffaf and not kati:
            return self._credentials_only(dict(headers))
        out = {}
        for k, v in headers.items():
            low = k.lower()
            if low in ("authorization", "proxy-authorization", "cookie", "set-cookie", "www-authenticate") or \
                    self._field_kind(low) == "secret":
                out[k] = "***"
            else:
                out[k] = self.mask_text(str(v), kati)
        return out
