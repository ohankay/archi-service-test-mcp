# Yeni bir sistem eklemek

Yeni bir servis için yeni bir MCP yazılmaz. Sistem bir tanım dosyasıdır; işlemleri de logdan çıkarılır.

## 1. Sistem tanımı

`definitions\systems\<ad>.yaml` dosyasını oluşturun. Örnek olarak `systems\mobile-banking.yaml` dosyasını kopyalayıp şu alanları uyarlayabilirsiniz:

| Alan | Ne yazılır |
|---|---|
| `environments` | Her ortam için `address` (işlem yolundan önceki kısım) ve o ortama özgü `variables`. İsteğe bağlı: `default_customer`, `allow_mutating`, bağlantı ayarları `timeout_s` (varsayılan 30), `proxy` (adres; `none` = doğrudan; `auto` = tarayıcı gibi Windows PAC ayarına göre), `verify_tls` (true / false / kurum CA .pem yolu). |
| `variables` | Tüm ortamlarda ortak değerler (`{{env.x}}`). |
| `request.url_pattern` | İşlemin URL'deki yeri. Varsayılan `"{path}/{method}"`. |
| `request.envelope` | Her istekte gönderilen alanlar. |
| `request.session_envelope` | Yalnızca bir müşteriyle oturum açıkken eklenen alanlar. |
| `request.headers` | Header şablonları. |
| `success` | İş seviyesinde başarı kuralı, kontrol diliyle (ör. `Success = true`). |
| `error_fields` | Hata kodu ve mesajının yanıttaki yolları (ör. `Results.ErrorCode`). |
| `session.login` | Login adımları: `method`, `path`, `input`, `extract` (yanıttan oturum değişkenine). Login yoksa boş bırakın. |
| `session.logout` | Logout adımı. |
| `session.ttl_s`, `session.expired_when` | Oturum süresi ve oturumun bittiğini gösteren yanıt kuralı. |
| `noise` | Logda yok sayılacak çağrılar (joker karakterle). |
| `secret_values` | Logdan yerel `secrets.yaml`'a alınacak değerler (`method` + `field`). |
| `log_fields` | Logda müşteri alanı (`customer`) ve gizli alan (`secret`) sayılacaklar. |

**Değişkenler:**
- `{{customer.no}}`: çalıştırırken verilen müşteri numarası.
- `{{session.X}}`: login yanıtlarından alınan değerler.
- `{{env.x}}`: ortam değişkenleri.
- `{{secret.x}}`: `STM_SECRET_X` ortam değişkeni, yoksa yerel `secrets.yaml`.
- `{{method}}`: çağrılan işlemin teknik adı.

Bir alanı bir adımda hiç göndermemek için değerine `null` yazın.

Şifre, token ve OTP'yi tanım dosyasına yazmayın. Ortam değişkenlerinde `"{{secret.x}}"` diye referans verin.

## 2. İşlemler

Uygulamayı ya da tarayıcıyı ilgili ekranlarda kullanıp logu alın, sonra ajana verin:

> "Bu logu import_log ile oku, yeni işlemleri göster."

Ajan taslakları gösterir. Siz şunları belirlersiniz:
- işlemlerin adları,
- hangi girdilerin önemli olduğu,
- girdilerin sınırları (`min`, `max`, `options`, `date_range`),
- işlemin türü (`kind`: `read` / `mutating`).

Taslaklar `apply=true` ile kaydedilir. İlk gerçek çağrıda (`call_operation`) yanıtın neye benzediği görülür ve kontroller buna göre yazılır.

## 3. Testler

- Hazır yaklaşımlarla üretin: "*X işlemini sınır değer ve negatif yaklaşımla tara*".
- Ya da tarif edin: "*Önce hesap listesini al, ilk hesabın son 30 günlük hareketlerini getir, tarihler aralıkta olsun*".

Ajan testi yazar, koşar ve onayınızla kaydeder. Onaylı (`status: approved`) testler `service-test-mcp --test all` ile CI'da da koşar.

## 4. YAML'ın yetmediği yerler

İmza, şifreleme ya da dinamik header gibi ihtiyaçlar için sistem tanımına `hook: hooks.py:Class` yazılır. Dosya, sistem tanımının yanında durur. Sınıfın `prepare(request, ctx)` metodu istek gönderilmeden önce, `inspect(result, ctx)` metodu yanıt geldikten sonra çalışır:

```python
# definitions/systems/hooks.py
class Signature:
    def __init__(self, profile):
        self.profile = profile

    def prepare(self, request, ctx):      # request: method, path, headers, body; ctx: operation, session ...
        request["headers"]["X-Signature"] = sign(request["body"], ctx["session"])

    def inspect(self, result, ctx):       # yanıt geldikten sonra (isteğe bağlı)
        pass
```

Ekip veya güvenlik ayrımı gerekiyorsa aynı kurulumla ikinci bir MCP sunucusu tanımlayın ve `STM_DEFINITIONS` ile ona ayrı bir tanım klasörü verin.
