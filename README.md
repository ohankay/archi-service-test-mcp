# archi-service-test-mcp (`service-test-mcp`)

**Servis katmanı testlerini bir AI ajanına yaptırmak için platform.** Merkezde bir MCP sunucusu var. VS Code'da GitHub Copilot (Agent modu) ya da bağımsız ajan (`service-test-agent`, kurum içi LLM) aynı araçları kullanır.

İlk hedef mobil bankacılık WCF servisleridir. Diğer servisler aynı koda **profil** olarak eklenir; yeni bir MCP yazmak gerekmez.

Temel aldığı proje [service-test-mcp-simple](https://github.com/ohankay/service-test-mcp-simple). SOAP/REST çağrıları, assertion motoru, maskeleme, denetim logu ve offline Windows kurulumu oradan geliyor. Bu repo üzerine şunları ekliyor:

| | |
|---|---|
| **Servis profilleri** | Bir servis ailesinin (ör. mobil WCF) nasıl çağrılacağı YAML'da tanımlanır: URL şablonu, her isteğe eklenen **zarf** (cihaz/uygulama/oturum alanları), header şablonları ve iş seviyesi başarı kuralı (`$.Success`). Ajan yalnızca operasyon adı ve parametre verir. |
| **Otomatik login ve session** | Login adımları profilde tanımlıdır (ör. uygulama girişi → müşteri girişi → sabit OTP). 5 dakikalık session süresi dolmadan yenilenir. Sunucu session'ı bitirirse yeniden login olunur ve okuma çağrısı bir kez tekrarlanır. Para hareketi otomatik tekrarlanmaz. |
| **Test müşterisi (`test_customer`)** | Sabit müşteri, müşteri havuzu ya da test-verisi API'sinden alınan müşteri. Aynı test müşterisi adı her ortamda o ortamın müşterisine çözülür. |
| **Paralel login kapalı** | Bir test müşterisiyle aynı anda tek login yapılabilir. Bunu süreçler ve makineler arası **test müşterisi kilidi** (süreli lease) sağlar: VS Code pencereleri, ajanlar ve CI birbirinin session'ını düşürmez. |
| **Logdan katalog** | Kullanıcı mobil uygulamanın transaction logunu (ya da HAR dosyasını) ajana verir. **Logu MCP okur**, model ham logu hiç görmez. Sunucu logu maskeler, servisi ve ortamı host'tan bulur ve yeni/değişen operasyonları, alan sınıflarını ve bağımlılıkları özetler. Kullanıcı onaylayınca katalog yazılır. İsteğe bağlı olarak akıştan taslak senaryo da üretilir. |
| **Ortamlar** | test / prep / dev: URL, OTP, test müşterisi ve test verisi ortam bazlıdır. Senaryolar ortamdan bağımsızdır (`--env prep`). Test ortamlarında para transferi dahil hiçbir şey engelli değildir. Prod adları reddedilir ve host allowlist korunur. |
| **İki çalışma modu** | VS Code + Copilot ve bağımsız ajan, aynı araçları ve aynı talimat dosyasını kullanır. CI için AI'sız koşu (`--run`, JUnit) vardır. |

Mimari, verilen kararlar ve açık konular: [docs/MIMARI.md](docs/MIMARI.md). Yeni bir servis ailesi eklemek için: [docs/YENI-SERVIS.md](docs/YENI-SERVIS.md).

## Hemen deneyin: mobil WCF demosu

Yerel demo sunucusu mobil uygulamanın konuştuğu yapıyı sentetik verilerle taklit eder: `…/<Modül>/<Servis>.svc/<Metod>`, JSON zarfı, üç adımlı login + OTP, 5 dakikalık session ve yeni login'in önceki session'ı düşürmesi.

```bat
service-test-mcp --demo-server
rem baska bir terminalde:
set STM_CONFIG=C:\Araclar\archi-service-test-mcp\examples\mobile\services.yaml
service-test-mcp --run C:\Araclar\archi-service-test-mcp\examples\mobile\scenarios --env test
service-test-mcp --run C:\Araclar\archi-service-test-mcp\examples\mobile\scenarios --env prep
service-test-mcp --import-log C:\Araclar\archi-service-test-mcp\examples\mobile\logs\ornek-prep-transaction.log
```

`examples/mobile` içindekiler:

| Dosya | İçerik |
|---|---|
| `services.yaml` | Ortamlar (test, prep), servis ve `profile:` bağlantısı |
| `profiles/mobil.profile.yaml` | Zarf, login akışı, session kuralları, başarı kuralı, ortam bazlı personalar |
| `profiles/mobil.catalog.yaml` | Logdan üretilmiş, elle açıklama eklenmiş operasyon kataloğu |
| `logs/ornek-prep-transaction.log` | Uygulamanın log formatında sentetik örnek log |
| `scenarios/` | Login + dashboard, para transferi + negatif, API'den gelen müşteri. `_taslak-*` dosyaları logdan üretilmiş taslaklardır; suite koşularında atlanır. |

## Kurulum (Windows, internet gerekmez)

1. Python'unuza uygun paketi indirip bir klasöre açın:
   - **64-bit Python:** `release/archi-service-test-mcp-windows-x64.zip`
   - **32-bit Python:** `release/archi-service-test-mcp-windows-x86.zip`

   Python'unuzun kaç bit olduğunu `py -c "import struct; print(struct.calcsize('P')*8)"` gösterir. Paketler `python scripts/build_bundle.py` ile üretilir (bkz. Geliştirme).
2. Klasörü VS Code'da açın ve Copilot Chat → **Agent** moduna şunu yazın:
   `KURULUM_REHBERI.md dosyasını oku ve "Ajan için kurulum adımları" bölümünü sırayla uygula.`

Elle kurulum için zip klasöründeki `install.cmd` dosyasını çalıştırın. Sonuç: `%USERPROFILE%\.service-test-mcp\Scripts\service-test-mcp.exe`.

İnternet varsa:

```bat
py -m venv %USERPROFILE%\.service-test-mcp
%USERPROFILE%\.service-test-mcp\Scripts\pip install --prefer-binary "archi-service-test-mcp @ git+https://github.com/ohankay/archi-service-test-mcp.git"
```

## VS Code'a ekleme

`Ctrl+Shift+P` → **MCP: Open User Configuration**:

```json
{
  "inputs": [
    { "type": "promptString", "id": "mobil-app-pw", "description": "Mobil uygulama kullanicisi sifresi", "password": true }
  ],
  "servers": {
    "service-test": {
      "type": "stdio",
      "command": "${userHome}\\.service-test-mcp\\Scripts\\service-test-mcp.exe",
      "env": {
        "STM_CONFIG": "${userHome}\\.service-test-mcp\\services.yaml",
        "STM_SECRET_MOBIL_APP_PASSWORD": "${input:mobil-app-pw}"
      }
    }
  }
}
```

Şifreyi VS Code bir kez sorar ve güvenli deposunda saklar. Profil bu şifreyi `{{secret.STM_SECRET_MOBIL_APP_PASSWORD}}` ile kullanır ve değeri hiçbir çıktıda görünmez.

Custom agent dosyası `.github/agents/service-tester.agent.md`. Bu dosya `src/service_test_mcp/prompts/service-tester.md` kaynağından üretilir ve bağımsız ajan da aynı dosyayı kullanır. Hazır görevler `.github/prompts/` altında, terminale kaçmayı önleyen talimatlar `templates/copilot-instructions.md` içinde.

## Mobil WCF profili nasıl çalışır

```yaml
# services.yaml
environments:
  test: { allow_mutating: true, variables: { otp: "123456" } }
  prep: { allow_mutating: true, variables: { otp: "123456" } }
services:
  MobilBankacilik:
    protocol: rest
    base_url:
      test: https://mobiltest.sirket.local/BOA.Integration.MobileBranch
      prep: https://mobilprep.sirket.local/BOA.Integration.MobileBranch
    profile: profiles/mobil.profile.yaml
```

Ajan şunu çağırır:

```
call_operation(service="MobilBankacilik", operation="GetDashBoardMobileInformation",
               params={"MainResourceCode": "DASHBOARD"}, test_customer="bireysel_standart")
```

Sunucu bu çağrı için sırasıyla şunları yapar:

1. Operasyonu katalogda bulur (`BOA.Integration.MobileBanking.DashBoard/DashBoardService.svc`).
2. `bireysel_standart` adlı test müşterisinin o ortamdaki karşılığını çözer ve onun kilidini alır.
3. Session yoksa ya da yaklaşık 4,5 dakikayı geçmişse login adımlarını koşar: `LoginForMobileBranch` → `InitialLogin` → `OtpLoginCompleteNew` (sabit OTP).
4. İsteği zarf + parametreler + header'lar ile gönderir; host allowlist, maskeleme ve denetim logu her çağrıda uygulanır.
5. `Success` alanına göre iş sonucunu döner. Sunucu `SESSION_EXPIRED` dönerse yeniden login olur ve okuma çağrısını bir kez tekrarlar.

Profilin tüm seçenekleri `src/service_test_mcp/profile/model.py` başındaki açıklamada, çalışan bir örnek `examples/mobile/profiles/mobil.profile.yaml` içinde.

**Personalar** (`list_test_customers` ile listelenir):

```yaml
environments:
  test:
    test_customers:
      bireysel_standart: { CustomerId: 1000001, UserId: "...", ActivationToken: "..." }   # sabit
      bireysel_havuz:                                                                       # havuz
        pool: [ { CustomerId: 1000002, ... }, { CustomerId: 1000003, ... } ]
      yeni_musteri:                                                                         # API'den
        source: { operation: GetTestCustomer, extract: { CustomerId: $.Value.CustomerId, ... } }
```

**Test müşterisi kilidi:** Kilitler varsayılan olarak `%USERPROFILE%\.service-test-mcp\locks` altında tutulur. Birden fazla makine aynı test müşterilerini paylaşıyorsa `locks.dir` ile ortak bir klasör (ağ paylaşımı) verin. Dolu bir test müşterisi istendiğinde havuzdan boş bir müşteri seçilir. Hiçbiri boş değilse `locks.wait_s` kadar beklenir, sonra kilidi kimin tuttuğunu söyleyen bir hata döner. Süreç çökerse kilit session süresi sonunda kendiliğinden düşer. `end_sessions` aracı logout yapar ve kilitleri bırakır.

## Logdan katalog

```
import_traffic_log(path="C:\\loglar\\TransactionLog.txt")              # önizleme
import_traffic_log(path="...", apply=true)                             # kullanıcı onayından sonra
import_traffic_log(path="...", scenario_path="scenarios/_taslak.yaml")  # akıştan taslak senaryo
```

Ajana dönen rapor şunları içerir:
- Operasyon durumları: yeni / değişti (alan eklendi, tip değişti) / değişmedi.
- Tahmini tür (read / mutating / auth / sınıflandırılamadı) ve session gerekip gerekmediği.
- Test müşterisi alanları ve kimlik bilgisi alanları (bunlar hiçbir yere yazılmaz).
- Önceki yanıtlardan gelen değerler (ör. `ExtUSessionKey ← InitialLogin $.SessionKey`).
- Zarfa taşınması önerilen alanlar, gürültü olarak atlanan çağrılar ve profil dışı kalan çağrılar (ör. chatbot API'si).

Ortam host'tan bulunur (prep host'u → prep). Host tanınmıyorsa tahmin yürütülmez, uyarı verilir. Elle yazılmış açıklama, tür ve etiketler sonraki importlarda korunur.

Desteklenen formatlar: mobil uygulamanın TransactionLog'u ve HAR. Yeni bir format, `importer/parsers.py` dosyasına eklenen tek bir fonksiyondur.

AI'sız kullanım: `service-test-mcp --import-log log.txt [--apply] [--scenario-out scenarios/_taslak.yaml]`.

## Araçlar

| Araç | Ne yapar |
|---|---|
| `list_services` | Servisler, ortamlar, ortam değişkenleri, profil özeti (login adımları, personalar) |
| `list_operations` | Profil servisinin katalogu: ad, tür, session, parametre adları; arama/tür/etiket filtresi |
| `describe_operation` | Profil: katalog girdisi (örnek parametreler, istek/yanıt şeması, tür). SOAP: şema, `example_params`. REST: OpenAPI |
| `call_operation` | Profil servisinde operasyon adıyla çağrı. Zarf, login, OTP ve session otomatik; isteğe bağlı `assertions` ve `extract` |
| `list_test_customers` | Personalar, bu sunucunun session'ları ve başka yerde (başka pencere, ajan, CI) kilitli personalar |
| `end_sessions` | Logout yapar ve test müşterisi kilitlerini bırakır |
| `import_traffic_log` | Logu sunucuda okur ve katalog önerisi sunar (önizleme / `apply`), taslak senaryo üretir |
| `describe_service`, `check_service` | Servis ayrıntısı ve bağlantı testi |
| `soap_call`, `rest_call` | Profilsiz SOAP/REST servisleri için ham çağrı |
| `session_variables`, `check_last_response` | `extract` değişkenleri; son yanıta assertion uygulama |
| `validate_scenario`, `run_scenario`, `run_test_suite` | YAML senaryoları doğrulama ve koşma, JUnit raporu |
| `reload_config` | `services.yaml` değişince yeniden okur |

## Senaryo formatı

```yaml
id: mobil-para-transferi
title: IBAN'a para transferi ve gecersiz tutar
tags: [transfer]
test_customer: bireysel_havuz             # senaryo geneli (adımda da verilebilir)
steps:
  - name: Bakiye
    service: MobilBankacilik         # profil servisi: operation + params
    operation: GetDashBoardMobileInformation
    params: { MainResourceCode: DASHBOARD }
    assert:
      - success: true
    extract: { oncekiBakiye: $.Value.Accounts[0].Balance }
  - name: Transfer
    service: MobilBankacilik
    operation: ExecuteMoneyTransfer
    params: { ToIban: "{{env.hedef_iban}}", Amount: "10.00", Description: test, TransferType: 1 }
    assert:
      - success: true
      - path: $.Value.NewBalance
        numeric: { lt: "{{oncekiBakiye}}" }
  - name: Sifir tutar reddedilir
    service: MobilBankacilik
    operation: ExecuteMoneyTransfer
    params: { ToIban: "{{env.hedef_iban}}", Amount: "0", Description: negatif, TransferType: 1 }
    assert:
      - success: false
      - path: $.Results[0].ErrorCode
        equals: INVALID_AMOUNT
```

Aynı dosyada SOAP adımları (`operation` + `params`) ve REST adımları (`method` + `path`) da kullanılabilir.

**Değişkenler:** `{{ad}}` (değişken ya da `extract` sonucu), `{{env.x}}` (ortam değişkeni; profilin ortam değişkenleri ve `services.yaml`), `{{test_customer.CustomerId}}`, `{{secret.STM_SECRET_X}}`, `{{$uuid}}`, `{{$today}}`, `{{$date(+1)}}`, `{{$random_digits(8)}}`.

**Assertion'lar:** `success: true|false` (profilin iş kuralı), `fault`, `status`, `responseTimeMs`, `header`, `schema` ve `path: <JSONPath>` ile birlikte bir operatör: `equals`, `notEquals`, `equalsIgnoreCase`, `contains`, `notContains`, `startsWith`, `endsWith`, `matches`, `in`, `notIn`, `exists`, `notEmpty`, `isNull`, `type`, `numeric`, `count`, `length`, `schema`. `all: true` assertion'ı her eşleşmeye uygular. Bir adım başarısız olursa sonraki adımlar atlanır; bunu `continue_on_failure: true` değiştirir.

Adı `_` ile başlayan senaryo dosyaları (ör. logdan üretilmiş taslaklar) suite koşularında atlanır.

## Bağımsız ajan (kurum içi LLM)

```bat
set STM_AGENT_BASE_URL=http://llm.sirket.local:8000/v1
set STM_AGENT_MODEL=kurum-modeli
set STM_CONFIG=%USERPROFILE%\.service-test-mcp\services.yaml
service-test-agent --check
service-test-agent --toolset profile --prompt "Mobil dashboard ve transferi bireysel_havuz ile test et" --output rapor.md
service-test-agent --toolset profile      &rem interaktif
```

- `--toolset profile` modele yalnızca profil araçlarını verir (11 araç). Küçük modeller için daha az seçenek, daha az hata demektir.
- Model sunucusunda tool calling açık olmalı (vLLM için örneğin `--enable-auto-tool-choice --tool-call-parser hermes`).
- Ajanın talimatları `src/service_test_mcp/prompts/service-tester.md` dosyasındadır; Copilot agent dosyası da aynı kaynaktan üretilir.

**HTTP üzerinden MCP:** `STM_HTTP_TOKEN=...` ayarlayıp `service-test-mcp --transport http --host 0.0.0.0 --port 8765` ile başlatın. Uç nokta `http://sunucu:8765/mcp`, istemci `Authorization: Bearer <token>` gönderir.

**AI'sız koşu (CI):** `service-test-mcp --run senaryolar --env prep --tags smoke --junit sonuc.xml`. Çıkış kodları: `0` hepsi geçti, `1` başarısız senaryo var, `2` yapılandırma hatası. Process normal biterse açık session'lar için logout yapılır.

## Bilinmesi gerekenler

- **Maskeleme:** TCKN, TR IBAN, kart numarası, telefon, e-posta ve şifre/token/secret/OTP/session key alanları maskelenir. Login'den gelen session değerleri ve logda görülen kimlik bilgileri, hangi alanda görünürlerse görünsünler gizlenir. Assertion'lar ve `extract` gerçek değerlerle çalışır.
- **Denetim logu:** `%USERPROFILE%\.service-test-mcp\logs\audit-YYYYMMDD.jsonl`. Login, logout, test müşterisi ve araç çağrılarının hepsi kaynağıyla (`origin`) birlikte kaydedilir.
- **Desteklenmeyen WCF binding'leri:** `netTcpBinding`, `netNamedPipeBinding` ve SecureConversation'lı `wsHttpBinding`. Mobil servisler `webHttpBinding` (JSON) kullandığı için profil ile çalışır.

## Açık konular

- **Header'lar:** Kimlik doğrulamada header'da taşınan değerler henüz netleşmedi. Profilde `request.headers` şablonu hazır (`{{session.AuthenticationToken}}` gibi değişkenleri kullanabilir); detaylar gelince doldurulacak.
- **Session sonu hata kodu:** Sunucunun session bittiğinde döndüğü kod `session.expired_when` ile tanımlanır. Demo `SESSION_EXPIRED` kullanıyor; gerçek kod öğrenilince profilde güncellenmeli.
- **Ortak test müşterileri:** Ekip ve CI aynı müşterileri farklı makinelerden kullanacaksa `locks.dir` ortak bir klasöre alınmalı. İleride bir DB veya Redis kilit deposu aynı arayüzle eklenebilir.

## Geliştirme

```bash
pip install -e ".[dev]"
pytest -v                                    # demo servislere karşı birim, entegrasyon, stdio/HTTP e2e, ajan testleri
python scripts/sync_agent_files.py           # ajan talimatı değişince Copilot agent dosyalarını üretir
pip install uv && python scripts/build_bundle.py   # release/*.zip (x64 + x86, Python 3.10-3.14)
```

## Lisans

MIT
