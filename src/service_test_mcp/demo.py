"""Local demo services so the tool can be tried (and tested) without real endpoints.

    service-test-mcp --demo-server            -> http://127.0.0.1:8899

SOAP (WCF basicHttpBinding style, document/literal wrapped, namespace http://tempuri.org/):
    /HesapServisi.svc?wsdl     Login, GetBakiye, TransferYap (mutating)
REST:
    GET  /api/                  health
    POST /api/auth/token        {"username","password"} -> {"access_token", ...}
    GET  /api/hesaplar/{no}     Bearer token required

Mobile WCF (JSON, see demo_mobile.py), with an optional environment prefix (/test, /prep):
    POST [/<env>]/MobileBranch/<Module>/<Service>.svc/<Method>

Demo users: kullanici "demo" / sifre "Demo123!". Token works for both SOAP and REST.
"""

from __future__ import annotations

import json
import threading
import uuid
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from . import demo_mobile

TNS = "http://tempuri.org/"
SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
USERS = {"demo": {"password": "Demo123!", "musteriNo": "1000001", "ad": "Ayşe Yılmaz", "tckn": "10000000146"}}
ACCOUNTS = {
    "1000001": [
        {"HesapNo": "1000001-1", "Iban": "TR330006100519786457841326", "Bakiye": Decimal("1500.50"), "DovizKodu": "TRY"},
        {"HesapNo": "1000001-2", "Iban": "TR320010009999901234567890", "Bakiye": Decimal("250.00"), "DovizKodu": "USD"},
    ]
}
TOKENS: dict[str, str] = {}
_LOCK = threading.Lock()

WSDL = """<?xml version="1.0" encoding="utf-8"?>
<wsdl:definitions name="HesapServisi" targetNamespace="http://tempuri.org/"
    xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/" xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
    xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:tns="http://tempuri.org/">
  <wsdl:types>
    <xs:schema elementFormDefault="qualified" targetNamespace="http://tempuri.org/">
      <xs:complexType name="LoginSonuc">
        <xs:sequence>
          <xs:element minOccurs="0" name="Basarili" type="xs:boolean"/>
          <xs:element minOccurs="0" name="Token" nillable="true" type="xs:string"/>
          <xs:element minOccurs="0" name="MusteriNo" nillable="true" type="xs:string"/>
        </xs:sequence>
      </xs:complexType>
      <xs:complexType name="Hesap">
        <xs:sequence>
          <xs:element minOccurs="0" name="HesapNo" type="xs:string"/>
          <xs:element minOccurs="0" name="Iban" type="xs:string"/>
          <xs:element minOccurs="0" name="Bakiye" type="xs:decimal"/>
          <xs:element minOccurs="0" name="DovizKodu" type="xs:string"/>
        </xs:sequence>
      </xs:complexType>
      <xs:complexType name="ArrayOfHesap">
        <xs:sequence>
          <xs:element minOccurs="0" maxOccurs="unbounded" name="Hesap" type="tns:Hesap"/>
        </xs:sequence>
      </xs:complexType>
      <xs:complexType name="BakiyeSonuc">
        <xs:sequence>
          <xs:element minOccurs="0" name="Basarili" type="xs:boolean"/>
          <xs:element minOccurs="0" name="MusteriNo" type="xs:string"/>
          <xs:element minOccurs="0" name="ToplamBakiyeTRY" type="xs:decimal"/>
          <xs:element minOccurs="0" name="SorguZamani" type="xs:dateTime"/>
          <xs:element minOccurs="0" name="Hesaplar" type="tns:ArrayOfHesap"/>
        </xs:sequence>
      </xs:complexType>
      <xs:complexType name="TransferSonuc">
        <xs:sequence>
          <xs:element minOccurs="0" name="Basarili" type="xs:boolean"/>
          <xs:element minOccurs="0" name="IslemNo" type="xs:string"/>
        </xs:sequence>
      </xs:complexType>
      <xs:element name="Login">
        <xs:complexType><xs:sequence>
          <xs:element minOccurs="1" name="kullaniciAdi" type="xs:string"/>
          <xs:element minOccurs="1" name="sifre" type="xs:string"/>
        </xs:sequence></xs:complexType>
      </xs:element>
      <xs:element name="LoginResponse">
        <xs:complexType><xs:sequence>
          <xs:element minOccurs="0" name="LoginResult" type="tns:LoginSonuc"/>
        </xs:sequence></xs:complexType>
      </xs:element>
      <xs:element name="GetBakiye">
        <xs:complexType><xs:sequence>
          <xs:element minOccurs="1" name="token" type="xs:string"/>
          <xs:element minOccurs="1" name="musteriNo" type="xs:string"/>
        </xs:sequence></xs:complexType>
      </xs:element>
      <xs:element name="GetBakiyeResponse">
        <xs:complexType><xs:sequence>
          <xs:element minOccurs="0" name="GetBakiyeResult" type="tns:BakiyeSonuc"/>
        </xs:sequence></xs:complexType>
      </xs:element>
      <xs:element name="TransferYap">
        <xs:complexType><xs:sequence>
          <xs:element minOccurs="1" name="token" type="xs:string"/>
          <xs:element minOccurs="1" name="kaynakHesapNo" type="xs:string"/>
          <xs:element minOccurs="1" name="hedefIban" type="xs:string"/>
          <xs:element minOccurs="1" name="tutar" type="xs:decimal"/>
        </xs:sequence></xs:complexType>
      </xs:element>
      <xs:element name="TransferYapResponse">
        <xs:complexType><xs:sequence>
          <xs:element minOccurs="0" name="TransferYapResult" type="tns:TransferSonuc"/>
        </xs:sequence></xs:complexType>
      </xs:element>
    </xs:schema>
  </wsdl:types>
  <wsdl:message name="LoginIn"><wsdl:part name="parameters" element="tns:Login"/></wsdl:message>
  <wsdl:message name="LoginOut"><wsdl:part name="parameters" element="tns:LoginResponse"/></wsdl:message>
  <wsdl:message name="GetBakiyeIn"><wsdl:part name="parameters" element="tns:GetBakiye"/></wsdl:message>
  <wsdl:message name="GetBakiyeOut"><wsdl:part name="parameters" element="tns:GetBakiyeResponse"/></wsdl:message>
  <wsdl:message name="TransferYapIn"><wsdl:part name="parameters" element="tns:TransferYap"/></wsdl:message>
  <wsdl:message name="TransferYapOut"><wsdl:part name="parameters" element="tns:TransferYapResponse"/></wsdl:message>
  <wsdl:portType name="IHesapServisi">
    <wsdl:operation name="Login">
      <wsdl:input message="tns:LoginIn"/><wsdl:output message="tns:LoginOut"/>
    </wsdl:operation>
    <wsdl:operation name="GetBakiye">
      <wsdl:input message="tns:GetBakiyeIn"/><wsdl:output message="tns:GetBakiyeOut"/>
    </wsdl:operation>
    <wsdl:operation name="TransferYap">
      <wsdl:input message="tns:TransferYapIn"/><wsdl:output message="tns:TransferYapOut"/>
    </wsdl:operation>
  </wsdl:portType>
  <wsdl:binding name="BasicHttpBinding_IHesapServisi" type="tns:IHesapServisi">
    <soap:binding transport="http://schemas.xmlsoap.org/soap/http"/>
    <wsdl:operation name="Login">
      <soap:operation soapAction="http://tempuri.org/IHesapServisi/Login" style="document"/>
      <wsdl:input><soap:body use="literal"/></wsdl:input><wsdl:output><soap:body use="literal"/></wsdl:output>
    </wsdl:operation>
    <wsdl:operation name="GetBakiye">
      <soap:operation soapAction="http://tempuri.org/IHesapServisi/GetBakiye" style="document"/>
      <wsdl:input><soap:body use="literal"/></wsdl:input><wsdl:output><soap:body use="literal"/></wsdl:output>
    </wsdl:operation>
    <wsdl:operation name="TransferYap">
      <soap:operation soapAction="http://tempuri.org/IHesapServisi/TransferYap" style="document"/>
      <wsdl:input><soap:body use="literal"/></wsdl:input><wsdl:output><soap:body use="literal"/></wsdl:output>
    </wsdl:operation>
  </wsdl:binding>
  <wsdl:service name="HesapServisi">
    <wsdl:port name="BasicHttpBinding_IHesapServisi" binding="tns:BasicHttpBinding_IHesapServisi">
      <soap:address location="__ADDRESS__"/>
    </wsdl:port>
  </wsdl:service>
</wsdl:definitions>
"""


def _envelope(body: str) -> bytes:
    return (f'<s:Envelope xmlns:s="{SOAP_ENV}"><s:Body>{body}</s:Body></s:Envelope>').encode("utf-8")


def _fault(code: str, message: str) -> bytes:
    return _envelope(f"<s:Fault><faultcode>s:{code}</faultcode><faultstring xml:lang=\"tr-TR\">{escape(message)}"
                     f"</faultstring><detail><HataKodu xmlns=\"{TNS}\">{code.upper()}-001</HataKodu></detail>"
                     "</s:Fault>")


def _login(user: str, password: str) -> str | None:
    info = USERS.get(user)
    if not info or info["password"] != password:
        return None
    token = uuid.uuid4().hex
    with _LOCK:
        TOKENS[token] = user
    return token


def _user_for(token: str | None) -> dict[str, Any] | None:
    with _LOCK:
        user = TOKENS.get(token or "")
    return USERS.get(user) if user else None


def _soap(xml: bytes) -> tuple[int, bytes]:
    from lxml import etree

    try:
        root = etree.fromstring(xml)
    except etree.XMLSyntaxError:
        return 400, _fault("Client", "Geçersiz XML")
    body = root.find(f"{{{SOAP_ENV}}}Body")
    if body is None or len(body) == 0:
        return 400, _fault("Client", "SOAP Body bulunamadı")
    req = body[0]
    op = etree.QName(req).localname

    def arg(name: str) -> str:
        el = req.find(f"{{{TNS}}}{name}")
        return (el.text or "") if el is not None else ""

    if op == "Login":
        token = _login(arg("kullaniciAdi"), arg("sifre"))
        if not token:
            return 500, _fault("Client", "Kullanıcı adı veya şifre hatalı")
        musteri = USERS[arg("kullaniciAdi")]["musteriNo"]
        return 200, _envelope(f'<LoginResponse xmlns="{TNS}"><LoginResult><Basarili>true</Basarili>'
                              f"<Token>{token}</Token><MusteriNo>{musteri}</MusteriNo></LoginResult></LoginResponse>")
    if op in ("GetBakiye", "TransferYap"):
        user = _user_for(arg("token"))
        if user is None:
            return 500, _fault("Client", "Oturum geçersiz veya süresi dolmuş")
        if op == "GetBakiye":
            if arg("musteriNo") != user["musteriNo"]:
                return 500, _fault("Client", "Müşteri bulunamadı")
            accounts = ACCOUNTS.get(user["musteriNo"], [])
            total = sum((a["Bakiye"] for a in accounts if a["DovizKodu"] == "TRY"), Decimal(0))
            items = "".join(f"<Hesap><HesapNo>{a['HesapNo']}</HesapNo><Iban>{a['Iban']}</Iban>"
                            f"<Bakiye>{a['Bakiye']}</Bakiye><DovizKodu>{a['DovizKodu']}</DovizKodu></Hesap>"
                            for a in accounts)
            return 200, _envelope(f'<GetBakiyeResponse xmlns="{TNS}"><GetBakiyeResult><Basarili>true</Basarili>'
                                  f"<MusteriNo>{user['musteriNo']}</MusteriNo><ToplamBakiyeTRY>{total}</ToplamBakiyeTRY>"
                                  "<SorguZamani>2026-10-26T10:00:00</SorguZamani>"
                                  f"<Hesaplar>{items}</Hesaplar></GetBakiyeResult></GetBakiyeResponse>")
        return 200, _envelope(f'<TransferYapResponse xmlns="{TNS}"><TransferYapResult><Basarili>true</Basarili>'
                              f"<IslemNo>TRX{uuid.uuid4().hex[:10].upper()}</IslemNo></TransferYapResult>"
                              "</TransferYapResponse>")
    return 500, _fault("Client", f"Bilinmeyen operasyon: {op}")


class _Handler(BaseHTTPRequestHandler):
    server_version = "DemoWCF/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        pass

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False,
                                      default=lambda o: float(o) if isinstance(o, Decimal) else str(o)).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _bearer(self) -> dict[str, Any] | None:
        auth = self.headers.get("Authorization", "")
        return _user_for(auth[7:]) if auth.startswith("Bearer ") else None

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path.lower() == "/hesapservisi.svc" and parts.query.lower() == "wsdl":
            host = self.headers.get("Host", f"127.0.0.1:{self.server.server_port}")
            wsdl = WSDL.replace("__ADDRESS__", f"http://{host}/HesapServisi.svc")
            return self._send(200, wsdl.encode("utf-8"), "text/xml; charset=utf-8")
        if parts.path in ("/api", "/api/"):
            return self._json(200, {"service": "demo-hesap-api", "status": "ok"})
        if parts.path.startswith("/api/hesaplar/"):
            user = self._bearer()
            if user is None:
                return self._json(401, {"error": "unauthorized", "message": "Bearer token gerekli"})
            no = parts.path.rsplit("/", 1)[-1]
            for acc in ACCOUNTS.get(user["musteriNo"], []):
                if acc["HesapNo"] == no:
                    return self._json(200, {
                        "hesapNo": acc["HesapNo"], "iban": acc["Iban"], "durum": "AKTIF",
                        "bakiye": float(acc["Bakiye"]), "dovizKodu": acc["DovizKodu"],
                        "musteri": {"musteriNo": user["musteriNo"], "ad": user["ad"], "tckn": user["tckn"]},
                        "hareketler": [{"tarih": "2026-10-20", "tutar": -120.25, "aciklama": "Market"},
                                       {"tarih": "2026-10-21", "tutar": 2000.00, "aciklama": "Maaş"}]})
            return self._json(404, {"error": "not_found", "message": f"Hesap bulunamadı: {no}"})
        return self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else b""
        if demo_mobile.BASE.strip("/") + "/" in parts.path:
            try:
                req = json.loads(data or b"{}", parse_float=Decimal)
            except ValueError:
                return self._json(400, {"error": "invalid_json"})
            # /test/MobileBranch/... and /prep/MobileBranch/... stand in for two environments (two hosts in reality)
            return self._json(*demo_mobile.handle(parts.path, req))
        if parts.path.lower() == "/hesapservisi.svc":
            status, body = _soap(data)
            return self._send(status, body, "text/xml; charset=utf-8")
        if parts.path == "/api/auth/token":
            try:
                req = json.loads(data or b"{}")
            except ValueError:
                return self._json(400, {"error": "invalid_json"})
            token = _login(str(req.get("username")), str(req.get("password")))
            if not token:
                return self._json(401, {"error": "invalid_grant", "message": "Kullanıcı adı veya şifre hatalı"})
            return self._json(200, {"access_token": token, "token_type": "Bearer", "expires_in": 900})
        return self._json(404, {"error": "not_found"})


def start(port: int = 0, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Start in a background thread (tests). Returns the server; ``server.server_port`` is the real port."""
    server = ThreadingHTTPServer((host, port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def serve_forever(port: int = 8899, host: str = "127.0.0.1") -> None:
    server = ThreadingHTTPServer((host, port), _Handler)
    print(f"Demo services on http://{host}:{port}  (SOAP: /HesapServisi.svc?wsdl  REST: /api/  "
          f"mobile WCF: {demo_mobile.BASE})  Ctrl+C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
