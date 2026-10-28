"""JSON Schemas of the four definition kinds: sistem, islem, yaklasim, test.

The same schemas validate what the agent saves (save_definition) and will drive the forms of the UI.
``service-test-mcp --schemas DIR`` writes them as JSON files.
"""

from __future__ import annotations

from typing import Any

_STR = {"type": "string"}
_STRS = {"type": "array", "items": {"type": "string"}}
_MAP = {"type": "object"}
_DURUM = {"type": "string", "enum": ["taslak", "onayli"], "description": "taslak: AI veya kişi tarafından üretildi, "
          "henüz onaylanmadı. onayli: CI ve toplu koşularda çalışır."}
_KONTROLLER = {"type": "array", "items": {"type": "string"},
               "description": "Kontrol satırları (ör. 'başarılı', 'X sayısı >= 1', 'her L: A <= B')"}

ADIM = {
    "type": "object", "additionalProperties": False, "required": ["metod"],
    "properties": {"metod": _STR, "yol": _STR, "girdi": _MAP, "aciklama": _STR,
                   "al": {"type": "object", "additionalProperties": _STR,
                          "description": "oturum değişkeni adı -> yanıttaki alan yolu"}},
}

SISTEM: dict[str, Any] = {
    "title": "Sistem", "type": "object", "additionalProperties": False, "required": ["ad", "ortamlar"],
    "properties": {
        "ad": _STR, "aciklama": _STR, "durum": _DURUM,
        "ortamlar": {"type": "object", "minProperties": 1, "additionalProperties": {
            "type": "object", "additionalProperties": False, "required": ["adres"],
            "properties": {"adres": _STR, "aciklama": _STR, "degiskenler": _MAP,
                           "degistirmeye_izin": {"type": "boolean", "description": "para transferi gibi işlemler"},
                           "varsayilan_musteri": {"type": ["string", "integer"]},
                           "zaman_asimi_sn": {"type": "number", "minimum": 1,
                                              "description": "yanıt için en fazla bekleme (varsayılan 30)"},
                           "proxy": {**_STR, "description": "ör. http://proxy.sirket.local:8080; 'yok' = "
                                     "HTTPS_PROXY gibi ayarları kullanma"},
                           "tls_dogrula": {"type": ["boolean", "string"],
                                           "description": "true / false / kurum CA dosyası (.pem) yolu"}}}},
        "degiskenler": {**_MAP, "description": "Tüm ortamlarda ortak değişkenler ({{ortam.x}})"},
        "istek": {"type": "object", "additionalProperties": False, "properties": {
            "metod": _STR, "adres_kalibi": _STR, "zarf": _MAP, "oturum_zarfi": _MAP, "basliklar": _MAP}},
        "basari": {**_KONTROLLER, "description": "İş seviyesinde başarı kuralı (ör. 'Success = true')"},
        "hata_alanlari": _STRS,
        "oturum": {"type": "object", "additionalProperties": False, "properties": {
            "sure_sn": {"type": "number", "minimum": 1}, "yenileme_payi_sn": {"type": "number", "minimum": 0},
            "sure_turu": {"type": "string", "enum": ["mutlak", "bosta"]},
            "baslangic": _MAP, "giris": {"type": "array", "items": ADIM}, "cikis": ADIM,
            "bitti_sayilir": _KONTROLLER, "degistirme_tekrar": {"type": "boolean"}}},
        "musteri": {"type": "object", "additionalProperties": False, "properties": {
            "alan": {**_STR, "description": "{{musteri.<alan>}} - varsayılan: no"}}},
        "gurultu": _STRS,
        "gizli_degerler": {"type": "object", "additionalProperties": {
            "type": "object", "required": ["metod", "alan"], "additionalProperties": False,
            "properties": {"metod": _STR, "alan": _STR}},
            "description": "Log içe aktarılırken yerel gizli.yaml'a alınacak değerler"},
        "log_alanlari": {"type": "object", "additionalProperties": False,
                         "properties": {"musteri": _STRS, "gizli": _STRS}},
        "kanca": {**_STR, "description": "YAML'ın yetmediği mantık için Python sınıfı: dosya.py:Sinif "
                  "(prepare(request, ctx) / inspect(result, ctx))"},
    },
}

GIRDI = {
    "type": "object", "additionalProperties": False, "required": ["alan"],
    "properties": {
        "alan": {**_STR, "description": "Servise giden alan adı"},
        "tip": {"type": "string", "enum": ["metin", "sayi", "ondalik", "tarih", "evet_hayir", "liste"]},
        "zorunlu": {"type": "boolean"}, "varsayilan": {}, "bicim": _STR, "aciklama": _STR,
        "en_az": {}, "en_fazla": {}, "secenekler": {"type": "array"},
        "kaynak": {"type": "object", "additionalProperties": False, "required": ["islem", "al"],
                   "properties": {"islem": _STR, "al": _STR, "girdi": _MAP},
                   "description": "Verilmezse bu işlemi çağırıp değeri yanıttan alır"},
    },
}

ISLEM: dict[str, Any] = {
    "title": "İşlem", "type": "object", "additionalProperties": False, "required": ["ad", "metod"],
    "properties": {
        "ad": _STR, "aciklama": _STR, "sistem": _STR, "metod": _STR, "yol": _STR, "http_metodu": _STR,
        "tur": {"type": "string", "enum": ["okuma", "degistirme", "giris", "bilinmiyor"]},
        "oturum": {"type": "boolean"}, "durum": _DURUM, "etiketler": _STRS,
        "girdiler": {"type": "object", "additionalProperties": GIRDI},
        "sabit": {**_MAP, "description": "Her çağrıda gönderilen sabit alanlar"},
        "tarih_araligi": {"type": "object", "additionalProperties": False, "required": ["baslangic", "bitis"],
                          "properties": {"baslangic": _STR, "bitis": _STR,
                                         "en_fazla_gun": {"type": "integer", "minimum": 0}}},
        "kontroller": {**_KONTROLLER, "description": "Bu işlemin her çağrısında geçerli kontroller"},
        "kaynak_log": _STR,
    },
}

ORNEK = {"type": "object", "required": ["durum", "beklenen"], "properties": {
    "durum": _STR, "beklenen": {"type": "string", "enum": ["başarılı", "hata", "?"]},
    "kontrol": _KONTROLLER, "not": _STR}}

TEST_ADIM = {
    "type": "object", "additionalProperties": False, "required": ["islem"],
    "properties": {"ad": _STR, "islem": _STR, "girdi": _MAP, "kontrol": _KONTROLLER, "ai_yorum": _STR,
                   "al": {"type": "object", "additionalProperties": _STR},
                   "beklenen": {"type": "string", "enum": ["başarılı", "hata", "?"]}},
}

TEST: dict[str, Any] = {
    "title": "Test", "type": "object", "additionalProperties": False, "required": ["test"],
    "properties": {
        "test": _STR, "aciklama": _STR, "etiketler": _STRS, "durum": _DURUM, "ortam": _STR,
        "degiskenler": _MAP,
        "adimlar": {"type": "array", "items": TEST_ADIM, "minItems": 1},
        "islem": {**_STR, "description": "Tablo testi: tek işlem, her satır bir durum"},
        "ornekler": {"type": "array", "items": ORNEK, "minItems": 1},
        "ortak_girdi": _MAP, "kontrol": _KONTROLLER, "ai_yorum": _STR,
        "yaklasim": _STR, "uretim": {"type": "string", "enum": ["elle", "otomatik", "ajan"]},
    },
    "oneOf": [{"required": ["adimlar"], "not": {"required": ["ornekler"]}},
              {"required": ["islem", "ornekler"], "not": {"required": ["adimlar"]}}],
}

YAKLASIM: dict[str, Any] = {
    "title": "Test yaklaşımı", "type": "object", "additionalProperties": False, "required": ["yaklasim", "uretici"],
    "properties": {
        "yaklasim": _STR, "diger_adlar": _STRS, "aciklama": _STR, "ne_zaman": _STR, "durum": _DURUM,
        "uretici": {"type": "string", "enum": ["sinir_deger", "esdeger", "pairwise", "negatif", "ajan"],
                    "description": "ajan dışındakiler kodla, deterministik üretir"},
        "kurallar": {**_STRS, "description": "ajan üreticisi için: testleri hangi kurallarla üreteceği"},
        "sinir": {"type": "integer", "minimum": 1, "description": "en fazla durum sayısı"},
        "beklenen_disari": {"type": "string", "enum": ["hata", "?"],
                            "description": "sınır dışı değerler için beklenen sonuç"},
    },
}

SEMALAR = {"sistem": SISTEM, "islem": ISLEM, "test": TEST, "yaklasim": YAKLASIM}
KLASORLER = {"sistem": "sistemler", "islem": "islemler", "test": "testler", "yaklasim": "yaklasimlar"}
AD_ALANI = {"sistem": "ad", "islem": "ad", "test": "test", "yaklasim": "yaklasim"}
