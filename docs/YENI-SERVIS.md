# Yeni bir sistem eklemek

Yeni bir servis için yeni bir MCP yazılmaz. Sistem bir tanım dosyasıdır; işlemleri de logdan çıkarılır.

## 1. Sistem tanımı

`tanimlar\sistemler\<ad>.yaml` dosyasını oluşturun. Örnek olarak `sistemler\mobil-bankacilik.yaml` dosyasını kopyalayıp şu alanları uyarlayabilirsiniz:

| Alan | Ne yazılır |
|---|---|
| `ortamlar` | Her ortam için `adres` (işlem yolundan önceki kısım) ve o ortama özgü `degiskenler`. İsteğe bağlı: `varsayilan_musteri`, `degistirmeye_izin`, bağlantı ayarları `zaman_asimi_sn` (varsayılan 30), `proxy` (adres; `yok` = doğrudan; `otomatik` = tarayıcı gibi Windows PAC ayarına göre), `tls_dogrula` (true / false / kurum CA .pem yolu). |
| `degiskenler` | Tüm ortamlarda ortak değerler (`{{ortam.x}}`). |
| `istek.adres_kalibi` | İşlemin URL'deki yeri. Varsayılan `"{yol}/{metod}"`. |
| `istek.zarf` | Her istekte gönderilen alanlar. |
| `istek.oturum_zarfi` | Yalnızca bir müşteriyle oturum açıkken eklenen alanlar. |
| `istek.basliklar` | Header şablonları. |
| `basari` | İş seviyesinde başarı kuralı, kontrol diliyle (ör. `Success = true`). |
| `hata_alanlari` | Hata kodu ve mesajının yanıttaki yolları (ör. `Results.ErrorCode`). |
| `oturum.giris` | Login adımları: `metod`, `yol`, `girdi`, `al` (yanıttan oturum değişkenine). Login yoksa boş bırakın. |
| `oturum.cikis` | Logout adımı. |
| `oturum.sure_sn`, `oturum.bitti_sayilir` | Oturum süresi ve oturumun bittiğini gösteren yanıt kuralı. |
| `gurultu` | Logda yok sayılacak çağrılar (joker karakterle). |
| `gizli_degerler` | Logdan yerel `gizli.yaml`'a alınacak değerler (`metod` + `alan`). |
| `log_alanlari` | Logda müşteri alanı ve gizli alan sayılacaklar. |

**Değişkenler:**
- `{{musteri.no}}`: çalıştırırken verilen müşteri numarası.
- `{{oturum.X}}`: login yanıtlarından alınan değerler.
- `{{ortam.x}}`: ortam değişkenleri.
- `{{gizli.x}}`: `STM_SECRET_X` ortam değişkeni, yoksa yerel `gizli.yaml`.
- `{{metod}}`: çağrılan işlemin teknik adı.

Bir alanı bir adımda hiç göndermemek için değerine `null` yazın.

Şifre, token ve OTP'yi tanım dosyasına yazmayın. Ortam değişkenlerinde `"{{gizli.x}}"` diye referans verin.

## 2. İşlemler

Uygulamayı ya da tarayıcıyı ilgili ekranlarda kullanıp logu alın, sonra ajana verin:

> "Bu logu import_log ile oku, yeni işlemleri göster."

Ajan taslakları gösterir. Siz şunları belirlersiniz:
- Türkçe adlar,
- hangi girdilerin önemli olduğu,
- girdilerin sınırları (`en_az`, `en_fazla`, `secenekler`, `tarih_araligi`),
- işlemin türü (okuma / değiştirme).

Taslaklar `apply=true` ile kaydedilir. İlk gerçek çağrıda (`call_operation`) yanıtın neye benzediği görülür ve kontroller buna göre yazılır.

## 3. Testler

- Hazır yaklaşımlarla üretin: "*X işlemini sınır değer ve negatif yaklaşımla tara*".
- Ya da tarif edin: "*Önce hesap listesini al, ilk hesabın son 30 günlük hareketlerini getir, tarihler aralıkta olsun*".

Ajan testi yazar, koşar ve onayınızla kaydeder. Onaylı testler `service-test-mcp --test hepsi` ile CI'da da koşar.

## 4. YAML'ın yetmediği yerler

İmza, şifreleme ya da dinamik header gibi ihtiyaçlar için sistem tanımına `kanca: kancalar.py:Sinif` yazılır. Dosya, sistem tanımının yanında durur. Sınıfın `prepare(request, ctx)` metodu istek gönderilmeden önce, `inspect(result, ctx)` metodu yanıt geldikten sonra çalışır:

```python
# tanimlar/sistemler/kancalar.py
class Imza:
    def __init__(self, profile):
        self.profile = profile

    def prepare(self, request, ctx):      # request: method, path, headers, body; ctx: operation, session ...
        request["headers"]["X-Imza"] = imzala(request["body"], ctx["session"])

    def inspect(self, result, ctx):       # yanıt geldikten sonra (isteğe bağlı)
        pass
```

Ekip veya güvenlik ayrımı gerekiyorsa aynı kurulumla ikinci bir MCP sunucusu tanımlayın ve `STM_TANIMLAR` ile ona ayrı bir tanım klasörü verin.
