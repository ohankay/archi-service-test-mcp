# archi-service-test-mcp (`service-test-mcp`)

**Servis katmanı testlerini bir AI ajanına yaptırmak için platform.** Teknik bilgisi az olan biri ajana kendi diliyle şunu yazabilir:

> "Hesap hareketleri ve kur listesi testlerini 12345678 müşterisiyle prep'te çalıştır."
>
> "Hesap hareketlerini sınır değer yaklaşımıyla tara."

Ajan bu isteği MCP sunucusunun araçlarıyla yerine getirir. Aynı araçları VS Code'da GitHub Copilot da, kurum içi bir LLM ile çalışan bağımsız ajan (`service-test-agent`) da kullanır. İlk hedef mobil bankacılık WCF servisleri; diğer servisler aynı yapıya tanım dosyası olarak eklenir.

Projedeki bütün adlandırmalar (araçlar, tanım anahtarları, sonuç alanları, ayarlar, dosya ve klasör adları) İngilizcedir. Test edilen servislerin kendi alan adları ve mesajları (`AccountNumber`, "Uygulama kullanıcısı doğrulanamadı" gibi) olduğu gibi kalır. Kullanıcı ajanla Türkçe konuşur; ajan Türkçe ifadeleri ("sınır değer", "hesap hareketleri") tanımlı adlarla eşler.

## Dört kavram

| Kavram | Ne | Kim yazar |
|---|---|---|
| **system** | Bir sisteme nasıl ulaşılır: ortam adresleri, login (müşteri no + ortak şifre + SMS OTP), her isteğe eklenen zarf (`envelope`) | Teknik bir kişi, bir kez |
| **operation** | "Account transactions" gibi bir servis işlemi; sade girdileri (`account_no`, `start`, `end`), varsayılanları ve sınırlarıyla | Logdan üretilir (`import_log`), insan onaylar |
| **approach** | Test tasarım tekniği: boundary value, equivalence, pairwise, negative, user journey… | QA lideri; yeni yaklaşım bir dosyadır |
| **test** | Adımlar (`steps`) ya da tablo (`operation` + `examples`) ve kontroller (`checks`) | Ajan üretir veya kişi yazar, insan onaylar |

Hepsi düz YAML dosyalarıdır ve `%USERPROFILE%\.service-test-mcp\definitions\` altında durur (`systems\`, `operations\`, `approaches\`, `tests\`). MCP güncellense de bu klasöre dokunulmaz. Dosyaların şemaları `service-test-mcp --schemas DIR` ile alınabilir; ileride yapılacak arayüz aynı dosyaları bu şemalarla düzenleyecek.

**Önceki sürümden geçiş:** Önceki sürüm tanımları Türkçe anahtarlarla `tanimlar\` klasöründe, gizli değerleri `gizli.yaml` dosyasında tutuyordu. Sunucu ilk açılışta (ve kurulum betiği `--migrate` ile) bunları bir kez yeni biçime çevirip `definitions\` ve `secrets.yaml`'a yazar. Ortam adresi, proxy, zaman aşımı gibi değişiklikleriniz korunur; hazır örneklerin adları yeni İngilizce adlarına eşlenir. Eski dosyalar yedek olarak yerinde kalır, artık okunmaz. Eski ayar adları (`STM_TOOLSET=temel/uzman`, `STM_SEFFAF` ...) `mcp.json` dosyalarında çalışmaya devam eder.

## Hazır gelenler

Kurulumla birlikte (`service-test-mcp --init`, kurulum betiği bunu kendisi çalıştırır) şunlar gelir:

- **System:** Mobile Banking. Gerçek uygulama logundaki login akışı birebir tanımlı: `LoginForMobileBranch` → `InitialLogin` → `SendSmsOtp` → `OtpLoginCompleteNew`.
- **Operations:** Home page, Account list, Account transactions, FX rates.
- **Tests:** Login, Account listing, Account transactions view, FX rate listing.
- **Approaches:** Boundary value analysis, Equivalence partitioning, Negative tests, Pairwise, User journey.

Sistem tanımında iki ortam var: yerel **demo** ve gerçek **prep** (`mobileprep.kuveytturk.com.tr`, logdaki cihaz ve uygulama bilgileriyle). **Şifreler ve OTP tanım dosyalarında yer almaz.** Bunlar ya ortam değişkeninden (`STM_SECRET_...`) okunur ya da yalnızca sizin bilgisayarınızdaki `secrets.yaml` dosyasından. Bu dosyayı `import_log(save_secrets=true)` kendi logunuzdan doldurur. Ek olarak kuruma özel bir tanım klasörü kullanılacaksa, zip klasörüne `company-definitions` adıyla konduğunda kurulum onu da yükler.

## Ajanın araçları (8)

| Araç | Ne yapar |
|---|---|
| `list_tests` | Tanımlı sistemleri, işlemleri (girdileriyle), testleri ve yaklaşımları listeler; tek bir tanımı sürümüyle (`version`) okur |
| `run_test` | Testi verilen müşteri numarası ve ortamla koşar: tek test, kaydedilmemiş taslak (`content`) ya da etikete göre toplu |
| `call_operation` | Tek bir işlemi adı ve sade girdileriyle dener |
| `generate_tests` | Bir yaklaşımla, işlemin tanımından **kodla** taslak tablo testi üretir (deterministik) |
| `save_definition` | İşlem, test, yaklaşım ya da sistem tanımını doğrular ve kaydeder (sürüm kontrollü) |
| `import_log` | Uygulama logunu (ya da HAR'ı) sunucuda okur ve tanımlı olmayan işlemler için taslak önerir |
| `check_connection` | Adrese giden her yolu (şu anki, doğrudan, Windows/tarayıcının seçtiği proxy) adım adım sınar (DNS → TCP → TLS → HTTP) ve nerede koptuğunu söyler |
| `end_sessions` | Oturumları kapatır ve test müşterilerini başkalarına bırakır |

Login, OTP, 5 dakikalık oturumun yenilenmesi ve zarf alanları sunucuda yapılır; ajan bunlarla uğraşmaz. Ama gizlenmez: her adım sonuçta görünür (aşağıda "Şeffaflık"). Aynı müşteriyle paralel login yapılmaz: başka bir pencere, ajan ya da CI o müşteriyi kullanıyorsa sunucu bunu söyler. Prod engeli ve denetim logu her çağrıda geçerlidir.

## Şeffaflık: kara kutu değil, cam kutu

Her sonuç `success`, `result` ve bir sorun varsa `problem` ile başlar: hangi adımda ve neden (ör. `login 4/4 (OtpLoginCompleteNew): HTTP 200, business result failure: ...`). HTTP çağrısı yapan her araç sonucunda ayrıca şu alanlar gelir:

| Alan | İçerik |
|---|---|
| `flow` | Her HTTP alışverişi için bir satır: hangi adım (`login 2/4`, `test`, `logout`), metod, kısa adres, HTTP durumu ya da hatanın aşaması, süre, iş sonucu, yanıttan alınan oturum değerleri |
| `trace` | Sorunlu alışverişlerin (hata, HTTP ≥ 400, iş sonucu başarısız) isteği ve yanıtı; başlıksız, gövdeler biçimi korunarak kısaltılmış. Ajanlar uzun araç sonuçlarını kesip dosyaya aktardığı için sonucun tamamı 8000 karakteri (`STM_RESULT_CHARS`) geçmez. `STM_TRACE=all` bütün alışverişleri başlıklarıyla gösterir (sınır yine geçerli). |
| `trace_file` | Kısaltılmamış izin JSON dosyası (bütün başlıklar, oturumun eklediği değerler, zaman aşımı, proxy, TLS ayarı): `%USERPROFILE%\.service-test-mcp\traces\` (son 200 dosya tutulur; `STM_TRACE_DIR`) |

Örnek `flow`:

```
1) [login 1/4] POST mobileprep.kuveytturk.com.tr/.../AuthenticationService.svc/LoginForMobileBranch -> HTTP 200, 412 ms, business result: success, extracted: session.SessionKey
2) [login 2/4] POST mobileprep.kuveytturk.com.tr/.../AuthenticationService.svc/InitialLogin -> ERROR: connected and the request was sent; the server did not answer in 30 s [ReadTimeout], 30004 ms
```

Hata olduğunda sonuçta hatanın türü (`kind`), aşaması (`phase`: DNS, TCP bağlantısı, TLS el sıkışması, proxy, yanıt beklerken ...), tam neden zinciri (`chain`) ve olası nedenler (`likely_causes`) yer alır. Sunucunun kendi kodunda beklenmeyen bir hata olursa hatanın oluştuğu dosya ve satırlar (`location`) da döner. Zaman aşımı ya da bağlantı hatasında `check_connection` her yolu (`routes`) adım adım sınar, hangisinin çalıştığını ve sistem tanımına yazılacak satırı (`suggestion`) söyler.

**Gizli değerler:** Varsayılan mod `STM_TRANSPARENCY=transparent`: yalnızca kimlik bilgileri (şifreler, OTP, `secrets.yaml` ve `STM_SECRET_...` değerleri) `«secret: ad»` etiketiyle gösterilir; müşteri no, IBAN, SessionKey, tutarlar gibi her şey olduğu gibi görünür. `STM_SHOW_SECRETS=1` kimlik bilgilerini de açık gösterir (yalnızca kendi bilgisayarınızda öğrenme amaçlı). `STM_TRANSPARENCY=masked` kişisel verileri de maskeler. Denetim logu her modda maskelidir.

**Bağlantı ayarları** sistem tanımında ortam başına yazılır; kaydedildiği anda geçerli olur (sunucuyu yeniden başlatmak gerekmez):

```yaml
environments:
  prep:
    address: https://mobileprep.kuveytturk.com.tr/BOA.Integration.MobileBranch
    timeout_s: 60                          # varsayılan 30
    proxy: auto                            # tarayıcı gibi (Windows PAC); ya da http://adres:port; none = doğrudan
    verify_tls: C:\company\company-ca.pem  # true / false / kurum CA dosyası
```

Uzman kullanım için `STM_TOOLSET=expert` ham SOAP/REST, profil ve YAML senaryo araçlarını da açar (toplam 24 araç).

## Deterministik kontrol ve AI yorumu

Kararı makine verir, yorumu AI yapar:

- **`checks:` satırları deterministiktir.** Sunucu bunları değerlendirir; sonuç her seferinde aynıdır ve CI'da AI olmadan koşar.
  ```
  success                                    failure                              duration < 3 s
  count AccountGroups.Accounts >= 1          Value.CustomerName is not empty
  AccountGroups.Accounts.IBANNumber matches ^TR\d{24}$                            error code = INVALID_DATE_RANGE
  for each FXRateModelList: CurrencyBid <= CurrencyAsk
  for each AccountTransactionHistoryList: TransactionDate between {{start}} and {{end}}
  ```
  Diğer işleçler: `=`, `!=`, `>`, `>=`, `<`, `<=`, `contains`, `does not contain`, `is empty`, `exists`, `missing`.

  Sonuçta yalnızca passed/failed değil, **her assertion** ayrı ayrı gelir; geçenler de. Her adım ve tablo satırı için `assertions` listesi ve `assertion_summary`:
  ```json
  {"check": "count AccountGroups.Accounts >= 1", "source": "step", "passed": true, "expected": "count >= 1", "actual": 2}
  ```
  `source` kontrolün nereden geldiğini söyler: `system` (sistemin iş başarı kuralı), `operation` (işlemin kendi kontrolleri), `test`, `step`, `example`. Sonuç sınırı aşacaksa önce tamamı `result_file`'a yazılır, sonra sırayla kısaltılır (önce geçen assertion'ların değerleri); kalan assertion'ların beklenen ve gerçek değeri hep kalır. CLI her assertion'ı `[PASS]`/`[FAIL]` satırı olarak yazar; JUnit raporunda her assertion ayrı bir testcase'tir. Alan yolları yanıttaki alan adlarıdır; listeler otomatik gezilir. Bakiye, kur, tarih gibi değişen veriler için sabit değer yerine **kural** yazılır.
- **`expected: ?` sonuçları gözlem (`observation`) olarak raporlanır.** Servisin ne yapması gerektiği bilinmiyorsa ajan karar vermez; davranışı size gösterir ve sorar. Onayladığınız sonuç teste yazılır ve o andan itibaren test deterministik koşar.
- **`ai_review:` sorularını ajan yanıtlar.** Örnek: "Hareket açıklamaları anlamlı mı?" Bu yanıt ayrı bir bölümde AI görüşü olarak gelir ve passed/failed kararını etkilemez.

## Test örnekleri

**Adımlı test.** `account_no`, ilk adımın yanıtından alınır:
```yaml
name: Account transactions view
steps:
  - operation: Account list
    extract: { account_no: "AccountGroups.Accounts[0].AccountNumber", account_suffix: "AccountGroups.Accounts[0].AccountSuffix" }
  - operation: Account transactions
    input: { account_no: "{{account_no}}", account_suffix: "{{account_suffix}}", start: today-30 }
    checks:
      - "for each AccountTransactionHistoryList: TransactionDate between {{start}} and {{end}}"
    ai_review: Are the transaction descriptions and amounts meaningful?
```

**Tablo testi.** "Sınır değerle tara" isteğine `generate_tests` böyle bir tablo üretir:
```yaml
name: Account transactions - Boundary value analysis
operation: Account transactions
examples:
  - { case: same day (0 days),          start: today,     end: today,   expected: success }
  - { case: end one day before start,   start: today,     end: today-1, expected: error }
  - { case: exactly 365 days,           start: today-365, end: today,   expected: success }
  - { case: 366 days (outside),         start: today-366, end: today,   expected: error }
  - { case: end date in the future,     start: today,     end: today+1, expected: "?" }
```

Değer ifadeleri: `today`, `today-7`, `today+1`; tarih biçimleri `DD.MM.YYYY` ve `YYYY-MM-DD` (saat: `HH:mm:ss`). Bir girdi `null` verilirse hiç gönderilmez (eksik alan testi). `{raw: "abc"}` biçimindeki değer tip dönüşümü yapılmadan, olduğu gibi gönderilir (yanlış tip testi).

## İşlem tanımı

```yaml
name: Account transactions
method: AccountTransactionHistoryList
path: BOA.Integration.MobileBanking.Account/AccountService.svc
kind: read                  # read | mutating | auth | unknown
inputs:
  account_no:     { field: AccountNumber, type: integer, required: true,
                    source: { operation: Account list, extract: "AccountGroups.Accounts[0].AccountNumber" } }
  account_suffix: { field: AccountSuffix, type: integer, default: 1, min: 1 }
  start:          { field: BeginDate, type: date, format: DD.MM.YYYY, default: today-7 }
  end:            { field: EndDate,   type: date, format: DD.MM.YYYY, default: today }
date_range: { start: start, end: end, max_days: 365 }
fixed: { MainResourceCode: MOBROTHRBN, AccountType: Cari }
```

Girdi tipleri: `text`, `integer`, `decimal`, `date`, `boolean`, `list` (`options` ile). `min`, `max`, `options` ve `date_range`, test üreticilerinin dayanağıdır. Bu bilgiler tanımda yoksa ajan size sorar ya da keşif çağrılarıyla bulup öneri olarak sunar.

## Kurulum ve VS Code

1. Python'unuza uygun zip'i (`release/archi-service-test-mcp-windows-x64.zip` ya da `-x86.zip`) bir klasöre açın.
2. Klasörü VS Code'da açın ve Copilot Chat → **Agent** moduna şunu yazın: `INSTALL_GUIDE.md dosyasını oku ve "Ajan için kurulum adımları" bölümünü sırayla uygula.`

   Ya da elle `install.cmd` çalıştırın. Kurulum yeri `%USERPROFILE%\.service-test-mcp` olur; tanımlar oraya kopyalanır.
3. `Ctrl+Shift+P` → **MCP: Open User Configuration**:
   ```json
   { "servers": { "archi-service-test": { "type": "stdio",
       "command": "${userHome}\\.service-test-mcp\\Scripts\\service-test-mcp.exe" } } }
   ```
4. Gizli değerleri yükleyin. Uygulamayı loglama modunda bir kez kullanın, sonra Copilot'a şunu yazın:
   > `C:\logs\TransactionLog.txt logunu import_log ile oku; gizli değerleri kaydetmeme izin veriyorum (save_secrets).`

   Uygulama şifreleri, ortak test şifresi ve OTP yerel `secrets.yaml` dosyasına yazılır; hiçbir çıktıda görünmez. Alternatif olarak bunları `mcp.json` içinde `STM_SECRET_APP_PASSWORD`, `STM_SECRET_APP_PASSWORD_2`, `STM_SECRET_CUSTOMER_PASSWORD` ve `STM_SECRET_OTP` ortam değişkenleriyle (`${input:...}`) de verebilirsiniz.
5. Deneyin: "*FX rate listing testini 12345678 ile prep'te çalıştır*".

Copilot custom agent ve hazır görevler: `.github/agents/service-tester.agent.md`, `.github/prompts/`. Agent dosyası, bağımsız ajanın da kullandığı tek talimat kaynağından (`src/service_test_mcp/prompts/service-tester.md`) üretilir.

## Demo (hiçbir yere bağlanmadan)

```bat
service-test-mcp --demo-server
rem baska terminalde:
service-test-mcp --init
service-test-mcp --test all --customer 1000001 --env demo
```

Demo; logdaki gerçek login akışını, hesap, hareket (tarih kurallarıyla) ve kur servislerini sentetik verilerle taklit eder.

## Bağımsız ajan ve CI

```bat
set STM_AGENT_BASE_URL=http://llm.company.local:8000/v1
set STM_AGENT_MODEL=company-model
service-test-agent --prompt "Hesap hareketlerini sınır değer yaklaşımıyla tara, 12345678 ile prep'te" --output report.md
```

Varsayılan olarak modele yalnızca 8 araç verilir (`basic`); küçük modeller için de uygundur. Model sunucusunda tool calling açık olmalıdır.

AI olmadan (CI, zamanlanmış koşu):
```bat
service-test-mcp --test all --tags smoke --customer 12345678 --env prep --junit result.xml
```
Yalnızca **onaylı** (`status: approved`) testler koşar. Çıkış kodları: `0` hepsi geçti (gözlemler dahil), `1` kalan test var, `2` yapılandırma hatası.

## Ayarlar

| Ortam değişkeni | Ne |
|---|---|
| `STM_HOME` | Kurulum ve veri klasörü (varsayılan `%USERPROFILE%\.service-test-mcp`) |
| `STM_DEFINITIONS` | Tanım klasörü (varsayılan `STM_HOME\definitions`) |
| `STM_SECRETS_FILE` | Yerel gizli değer dosyası (varsayılan `STM_HOME\secrets.yaml`) |
| `STM_SECRET_<AD>` | Bir gizli değer (`{{secret.<ad>}}`) |
| `STM_TOOLSET` | `basic` (8 araç, varsayılan) ya da `expert` (24 araç) |
| `STM_TRANSPARENCY` | `transparent` (varsayılan) ya da `masked` |
| `STM_SHOW_SECRETS` | `1`: kimlik bilgilerini de göster |
| `STM_TRACE` | `all`: sonuçta bütün alışverişleri göster |
| `STM_TRACE_DIR`, `STM_TRACE_CHARS`, `STM_TRACE_BODY_CHARS`, `STM_RESULT_CHARS` | İz dosyalarının yeri ve sonuçtaki boyut sınırları |
| `STM_CONFIG` | Uzman katman için `services.yaml` |

## Bilinmesi gerekenler

- **Logdan yanıt alınmaz.** Bazı uygulama logları, aynı anda yapılan çağrıların yanıtlarını yanlış metoda yazıyor. Bu yüzden yanıt alanları ilk gerçek çağrıda görülür; `import_log` yalnızca istekleri ve akışı kullanır.
- **Açık konular:** Header'da taşınan değerler henüz belirlenmedi; sistem tanımında `request.headers` alanı hazır bekliyor. Sunucunun oturum bittiğinde döndüğü hata kodu da öğrenilince `session.expired_when` alanına yazılmalı (demo `SESSION_EXPIRED` kullanıyor).
- **Ayrıntılar:** Mimari [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), yeni bir sistem eklemek [docs/NEW-SYSTEM.md](docs/NEW-SYSTEM.md). Uzman araç setinin profil ve senaryo biçimleri `src/service_test_mcp/profile/model.py` ile `scenario/model.py` dosyalarının başındaki açıklamalarda.

## Geliştirme

```bash
pip install -e ".[dev]"
pytest -v
python scripts/sync_agent_files.py                 # ajan talimatı değişince
pip install uv && python scripts/build_bundle.py   # release/*.zip
```

## Lisans

MIT
