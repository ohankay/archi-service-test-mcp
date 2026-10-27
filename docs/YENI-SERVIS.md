# Yeni bir servis ailesi eklemek

Yeni bir servis için yeni bir MCP yazılmaz. Sırasıyla şunlar yapılır:

1. **Servisin türünü belirleyin.**
   - **HTTP + JSON, operasyon adıyla çağrılan servisler** (WCF `webHttpBinding`, kurum içi RPC tarzı API'ler): profil kullanın.
   - **Klasik SOAP (WSDL var)** ve **OpenAPI'li REST** servisleri: profilsiz de çalışır (`soap_call` / `rest_call`, `describe_operation`).
2. **`services.yaml` dosyasına servisi ekleyin.** Ortam bazlı `base_url` ve `profile:` alanlarını verin:

   ```yaml
   services:
     KrediApi:
       protocol: rest
       base_url:
         test: https://kredi-test.sirket.local/api
         prep: https://kredi-prep.sirket.local/api
       profile: profiles/kredi.profile.yaml
   ```

3. **Profil dosyasını yazın.** `examples/mobile/profiles/mobil.profile.yaml` dosyasını kopyalayıp şu alanları uyarlayın:
   - `request.url`: Operasyonun URL'deki yeri. Örneğin `"{path}/{operation}"` ya da `"v1/{operation}"`. `{path}` katalogdaki yoldur.
   - `request.method`: Varsayılan HTTP metodu. Katalogda operasyon bazında değiştirilebilir.
   - `request.envelope` / `session_envelope` / `headers`: Her isteğe eklenecek alanlar. Kullanılabilecek değişkenler `{{operation}}`, `{{session.X}}`, `{{test_customer.X}}`, `{{env.X}}` ve `{{secret.STM_SECRET_X}}`.
   - `success`, `error_paths`: İş seviyesinde başarı kuralı ve hata mesajlarının yolları.
   - `session`: Login adımları ve bunların `extract` alanları, `ttl_s`, `expired_when`, `logout`. Login gerektirmeyen servislerde `login` listesini boş bırakın.
   - `test_customers` ve `environments.<env>.test_customers`: Test müşterileri ya da kullanıcıları. Gerekmiyorsa boş bırakın.
   - `noise`, `importer.test_customer_fields`, `importer.secret_fields`: Log importu için.
   - `catalog`: Katalog dosyasının yolu. Başlangıçta boş olabilir: `operations: {}`.
4. **Kataloğu doldurun.** Uygulamanın ya da tarayıcının (HAR) logunu ajana verin: `import_traffic_log(path)`. Önizlemeyi inceleyin, `apply=true` ile yazın. Ardından `kind` alanı `unknown` kalan operasyonları sınıflandırın ve açıklama ekleyin.
5. **Deneyin.** `call_operation` ile tek çağrı yapın, sonra senaryo yazıp `run_test_suite --env test` ile koşun.
6. **YAML'ın yetmediği yerlerde hook yazın.** İmza, şifreleme ya da dinamik header gibi ihtiyaçlar için:

   ```python
   # profiles/hooks.py      ->  profilde: hooks: hooks.py:KrediHooks
   class KrediHooks:
       def __init__(self, profile):
           self.profile = profile

       def prepare(self, request, ctx):        # request: method, path, headers, body
           request["headers"]["X-Signature"] = imzala(request["body"], ctx["session"])

       def inspect(self, result, ctx):         # yanıt sonrası (ör. ek iş kuralı)
           pass
   ```

Ekip veya güvenlik ayrımı gerekiyorsa (ör. bir ekip yalnızca kendi servislerini görsün), aynı kurulumla ikinci bir MCP sunucusu tanımlayın ve `STM_CONFIG` ile farklı bir `services.yaml` gösterin. Kod değişmez.
