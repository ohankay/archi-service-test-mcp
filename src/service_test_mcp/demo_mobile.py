"""Synthetic mobile-banking WCF (webHttpBinding, JSON) service for the demo, the tests and trying out profiles.

It behaves like the real kind of service the mobile profile targets, with made-up data:

* POST ``[/<env>]/MobileBranch/<Module>/<Service>.svc/<Method>`` with a JSON body (the optional ``/test`` or
  ``/prep`` prefix lets one demo stand in for two environments); every body carries an envelope
  (``MethodName`` must equal the method, ``DeviceId`` must be set, ``ExtUSessionKey`` carries the session).
* Login in three steps: ``LoginForMobileBranch`` (application credentials) -> ``InitialLogin`` (customer +
  activation token) -> ``OtpLoginCompleteNew`` (fixed OTP on test environments).
* Sessions end after ``SESSION_TTL_S`` seconds, and a new login of the same customer ends the previous session
  (parallel logins are not allowed). Both answer HTTP 200 with ``Success: false`` and ErrorCode SESSION_EXPIRED.
* ``TestData/TestDataService.svc/GetTestCustomer`` hands out test customers (for API-sourced test_customers).

Demo values (never real): application user ``MobileTestApp`` / ``Demo-App-Pass1``, OTP ``123456``.
"""

from __future__ import annotations

import threading
import time
import uuid
from decimal import Decimal
from typing import Any

BASE = "/MobileBranch/"
APP_USER = "MobileTestApp"
APP_PASSWORD = "Demo-App-Pass1"
OTP = "123456"
SESSION_TTL_S = 300.0
CUSTOMERS: dict[str, dict[str, Any]] = {
    "1000001": {"UserId": "U1-a8f3", "ActivationToken": "ACT-1000001-x9", "Name": "Ayşe Yılmaz",
                "Iban": "TR330006100519786457841326", "Balance": Decimal("1500.50")},
    "1000002": {"UserId": "U2-b7e1", "ActivationToken": "ACT-1000002-k4", "Name": "Mehmet Demir",
                "Iban": "TR320010009999901234567890", "Balance": Decimal("820.00")},
    "1000003": {"UserId": "U3-c2d9", "ActivationToken": "ACT-1000003-p7", "Name": "Zeynep Kaya",
                "Iban": "TR120006200000000123456789", "Balance": Decimal("50.25")},
    # used by the prep environment of the example (the demo serves both environments)
    "2000001": {"UserId": "P1-d4a2", "ActivationToken": "ACT-2000001-m3", "Name": "Ali Çelik",
                "Iban": "TR560006400000011111111111", "Balance": Decimal("990.00")},
    "2000002": {"UserId": "P2-e5b3", "ActivationToken": "ACT-2000002-n8", "Name": "Elif Şahin",
                "Iban": "TR780001000000022222222222", "Balance": Decimal("310.40")},
    "2000003": {"UserId": "P3-f6c4", "ActivationToken": "ACT-2000003-q1", "Name": "Can Öztürk",
                "Iban": "TR900004600000033333333333", "Balance": Decimal("75.00")},
    # handed out by GetTestCustomer (the test data API never gives a customer that is configured elsewhere)
    "3000001": {"UserId": "T1-a1a1", "ActivationToken": "ACT-3000001-t1", "Name": "Deniz Ak",
                "Iban": "TR110001200000044444444444", "Balance": Decimal("500.00")},
    "3000002": {"UserId": "T2-b2b2", "ActivationToken": "ACT-3000002-t2", "Name": "Ece Ay",
                "Iban": "TR220001200000055555555555", "Balance": Decimal("500.00")},
    "3000003": {"UserId": "T3-c3c3", "ActivationToken": "ACT-3000003-t3", "Name": "Umut Er",
                "Iban": "TR330001200000066666666666", "Balance": Decimal("500.00")},
}
_BALANCES = {k: v["Balance"] for k, v in CUSTOMERS.items()}
SESSIONS: dict[str, dict[str, Any]] = {}
ACTIVE: dict[str, str] = {}          # customer -> session key of its live session
STATS = {"app_logins": 0, "customer_logins": 0, "transfers": 0, "ended_by_parallel_login": 0}
_LOCK = threading.Lock()
_next_test_customer = [0]

PUBLIC = {"ApplicationPreData", "LoginForMobileBranch", "InitialLogin", "GetTestCustomer", "InsertClientLog"}


def reset() -> None:
    with _LOCK:
        SESSIONS.clear()
        ACTIVE.clear()
        for k in STATS:
            STATS[k] = 0
        _next_test_customer[0] = 0
        for k, v in _BALANCES.items():
            CUSTOMERS[k]["Balance"] = v


def _error(code: str, message: str) -> dict[str, Any]:
    return {"Success": False, "Results": [{"ErrorCode": code, "ErrorMessage": message}]}


def _ok(**values: Any) -> dict[str, Any]:
    return {"Success": True, "Results": [], **values}


def _session(key: str, stage: str = "active") -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    with _LOCK:
        s = SESSIONS.get(key or "")
        if s is None:
            return None, _error("SESSION_EXPIRED", "Oturum bulunamadı veya sonlandırıldı")
        if time.time() - s["created"] > SESSION_TTL_S:
            SESSIONS.pop(key, None)
            return None, _error("SESSION_EXPIRED", "Oturum süresi doldu")
        if s["stage"] != stage:
            return None, _error("SESSION_STAGE", f"Bu işlem için oturum aşaması '{stage}' olmalı")
        return s, None


def handle(path: str, body: Any) -> tuple[int, dict[str, Any]]:
    if not path.startswith(BASE):
        return 404, {"error": "not_found"}
    method = path.rstrip("/").rsplit("/", 1)[-1]
    if not isinstance(body, dict):
        return 400, {"error": "invalid_json"}
    if body.get("MethodName") != method:
        return 200, _error("ENVELOPE", "MethodName zarf alanı metod adıyla aynı olmalı")
    if not body.get("DeviceId"):
        return 200, _error("ENVELOPE", "DeviceId zorunlu")
    key = str(body.get("ExtUSessionKey") or "")

    if method == "ApplicationPreData":
        return 200, _ok(Value={"MinVersion": "7.0.0", "Maintenance": False})
    if method == "InsertClientLog":
        return 200, _ok()
    if method == "GetTestCustomer":
        with _LOCK:
            ids = sorted(k for k in CUSTOMERS if k.startswith("3"))
            cid = ids[_next_test_customer[0] % len(ids)]
            _next_test_customer[0] += 1
        c = CUSTOMERS[cid]
        return 200, _ok(Value={"CustomerId": int(cid), "UserId": c["UserId"], "ActivationToken": c["ActivationToken"]})
    if method == "LoginForMobileBranch":
        if body.get("ExtUName") != APP_USER or body.get("ExtUPassword") != APP_PASSWORD:
            return 200, _error("APP_LOGIN_FAILED", "Uygulama kullanıcısı doğrulanamadı")
        new = uuid.uuid4().hex
        with _LOCK:
            SESSIONS[new] = {"stage": "app", "created": time.time(), "customer": None}
            STATS["app_logins"] += 1
        return 200, _ok(SessionKey=new, SessionId=str(uuid.uuid4()), IsCaptchaRequired=False)
    if method == "InitialLogin":
        cid = str(body.get("CustomerId") or "")
        c = CUSTOMERS.get(cid)
        if c is None or body.get("ActivationToken") != c["ActivationToken"]:
            return 200, _error("LOGIN_FAILED", "Müşteri bilgileri doğrulanamadı")
        new = uuid.uuid4().hex
        with _LOCK:
            old = ACTIVE.get(cid)
            if old and old in SESSIONS:
                SESSIONS.pop(old)
                STATS["ended_by_parallel_login"] += 1
            SESSIONS[new] = {"stage": "otp", "created": time.time(), "customer": cid}
            ACTIVE[cid] = new
            STATS["customer_logins"] += 1
        return 200, _ok(SessionKey=new, SessionId=str(uuid.uuid4()), CustomerId=int(cid),
                        AuthenticationToken="eyJ.demo." + uuid.uuid4().hex, IsCustomerPrivate=False)
    if method == "OtpLoginCompleteNew":
        s, err = _session(key, "otp")
        if err:
            return 200, err
        if str(body.get("GeneratedOtp")) != OTP:
            return 200, _error("OTP_INVALID", "Doğrulama kodu hatalı")
        with _LOCK:
            s["stage"] = "active"
        c = CUSTOMERS[s["customer"]]
        return 200, _ok(LoginResponse={"CustomerName": c["Name"], "CustomerId": int(s["customer"])})

    s, err = _session(key)
    if err:
        return 200, err
    c = CUSTOMERS[s["customer"]]
    if method == "GetDashBoardMobileInformation":
        account = {"AccountNumber": f"{s['customer']}-1", "Iban": c["Iban"], "Balance": c["Balance"],
                   "CurrencyCode": "TRY"}
        return 200, _ok(Value={"CustomerName": c["Name"], "Accounts": [account]})
    if method == "GetProfileBasics":
        return 200, _ok(Value={"CustomerId": int(s["customer"]), "Name": c["Name"], "Email": "musteri@example.com"})
    if method == "ExecuteMoneyTransfer":
        try:
            amount = Decimal(str(body.get("Amount")))
        except Exception:  # noqa: BLE001
            amount = Decimal(0)
        if amount <= 0:
            return 200, _error("INVALID_AMOUNT", "Tutar sıfırdan büyük olmalı")
        if amount > c["Balance"]:
            return 200, _error("INSUFFICIENT_BALANCE", "Bakiye yetersiz")
        with _LOCK:
            c["Balance"] -= amount
            STATS["transfers"] += 1
        return 200, _ok(Value={"TransactionId": "TRX" + uuid.uuid4().hex[:10].upper(), "Amount": amount,
                               "NewBalance": c["Balance"]})
    if method == "Logout":
        with _LOCK:
            SESSIONS.pop(key, None)
            if ACTIVE.get(s["customer"]) == key:
                ACTIVE.pop(s["customer"], None)
        return 200, _ok()
    return 200, _error("UNKNOWN_METHOD", f"Bilinmeyen metod: {method}")


# -- sample traffic log (the mobile app's TransactionLog format) ------------------------------------------------------
SEPARATOR = "*" * 65
MODULES = {
    "ApplicationPreData": "Base/BaseService.svc",
    "LoginForMobileBranch": "Authentication/AuthenticationService.svc",
    "InitialLogin": "Authentication/AuthenticationService.svc",
    "OtpLoginCompleteNew": "Authentication/AuthenticationService.svc",
    "Logout": "Authentication/AuthenticationService.svc",
    "InsertClientLog": "ClientLog/ClientLogService.svc",
    "GetDashBoardMobileInformation": "DashBoard/DashBoardService.svc",
    "GetProfileBasics": "Settings/SettingsService.svc",
    "ExecuteMoneyTransfer": "MoneyTransfer/MoneyTransferService.svc",
    "GetTestCustomer": "TestData/TestDataService.svc",
}


def record_sample_log(base_url: str, customer: str = "1000001", amount: str = "25.50") -> str:
    """Run the app's typical flow against the demo service and return it as a TransactionLog text.

    Like real logs, one request has no logged response and one response is logged twice.
    """
    import datetime as _dt
    import json

    import requests

    c = CUSTOMERS[customer]
    envelope = {"DeviceId": "7D1E7C1E-0000-4000-8000-00000000A001", "AppName": "IOS MobileBranch", "Version": "7.2.5",
                "Platform": 3, "DeviceOSName": "IOS", "DeviceOSVersion": "26.5.2", "DeviceModel": "iPhone 15 Pro Max",
                "LanguageId": 1, "SessionType": 1, "IsDeviceRooted": False, "ExtUName": APP_USER,
                "ExtUPassword": APP_PASSWORD}
    session = {"key": ""}
    out: list[str] = []
    clock = [_dt.datetime(2026, 10, 27, 10, 15, 0)]

    def block(lines: list[str], kind: str, data: Any) -> None:
        clock[0] += _dt.timedelta(milliseconds=700)
        out.append(SEPARATOR)
        out.append(f"Date: {clock[0]:%d-%m-%Y %H:%M:%S}")
        out.extend(lines)
        out.append(f"{kind}: " + json.dumps(data, ensure_ascii=False, indent=2, default=str).replace("/", "\\/"))
        out.append("")

    def call(method: str, params: dict[str, Any] | None = None, log_response: int = 1,
             owner: bool = True) -> dict[str, Any]:
        url = f"{base_url.rstrip('/')}/{MODULES[method]}/{method}"
        body = {**envelope, "MethodName": method, "ExtUSessionKey": session["key"], **(params or {})}
        if owner and session["key"]:
            body.setdefault("OwnerCustomerId", customer)
        block(["Method: post", f"Method Name: {method}", f"URL: {url}"], "RequestData", body)
        resp = requests.post(url, data=json.dumps(body, default=str), timeout=10,
                             headers={"Content-Type": "application/json"}).json()
        for _ in range(log_response):
            block([f"Method Name: {method}", f"URL: {url}"], "ResponseData", resp)
        return resp

    call("ApplicationPreData")
    session["key"] = call("LoginForMobileBranch", {"UserId": c["UserId"]})["SessionKey"]
    call("InsertClientLog", {"LogText": "app started"}, log_response=0)
    session["key"] = ""
    login = call("InitialLogin", {"CustomerId": customer, "ActivationToken": c["ActivationToken"]})
    session["key"] = login["SessionKey"]
    call("OtpLoginCompleteNew", {"GeneratedOtp": OTP, "CustomerId": customer})
    call("GetDashBoardMobileInformation", {"MainResourceCode": "DASHBOARD"}, log_response=2)
    call("GetProfileBasics")
    call("ExecuteMoneyTransfer", {"ToIban": "TR320010009999901234567890", "Amount": amount,
                                  "Description": "kira", "TransferType": 1})
    call("Logout")
    return "\n" + "\n".join(out) + "\n"
