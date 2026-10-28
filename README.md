# archi-service-test-mcp (`service-test-mcp`)

**Servis katmanı testlerini bir AI ajanına yaptırmak için platform.** Teknik bilgisi az olan biri ajana Türkçe olarak şunu yazabilir:

> "Hesap hareketleri ve kur listesi testlerini 12345678 müşterisiyle prep'te çalıştır."
>
> "Hesap hareketlerini sınır değer yaklaşımıyla tara."

Ajan bu isteği MCP sunucusunun araçlarıyla yerine getirir. Aynı araçları VS Code'da GitHub Copilot da, kurum içi bir LLM ile çalışan bağımsız ajan (`service-test-agent`) da kullanır. İlk hedef mobil bankacılık WCF servisleri; diğer servisler aynı yapıya tanım dosyası olarak eklenir.

## Dört kavram

| Kavram | Ne | Kim yazar |
|---|---|---|
| **Sistem** | Bir sisteme nasıl ulaşılır: ortam adresleri, login (müşteri no + ortak şifre + SMS OTP), her isteğe eklenen zarf | Teknik bir kişi, bir kez |
| **İşlem** | "Hesap hareketleri" gibi bir operasyon; sade girdileri (`hesap_no`, `baslangic`, `bitis`), varsayılanları ve sınırlarıyla | Logdan üretilir (`import_log`), insan onaylar |
| **Yaklaşım** | Test tasarım tekniği: sınır değer, eşdeğerlik, pairwise, negatif, kullanıcı yolculuğu… | QA lideri; yeni yaklaşım bir dosyadır |
| **Test** | Adımlar (`adimlar`) ya da tablo (`islem` + `ornekler`) ve kontroller | Ajan üretir veya kişi yazar, insan onaylar |

Hepsi düz YAML dosyalarıdır ve `%USERPROFILE%\.service-test-mcp\tanimlar\` altında durur (`sistemler\`, `islemler\`, `yaklasimlar\`, `testler\`). MCP güncellense de bu klasöre dokunulmaz. Dosyaların şemaları `service-test-mcp --schemas DIR` ile alınabilir; ileride yapılacak arayüz aynı dosyaları bu şemalarla düzenleyecek.

## Hazır gelenler

Kurulumla birlikte (`service-test-mcp --init`, kurulum betiği bunu kendisi çalıştırır) şunlar gelir:

- **Sistem:** Mobil Bankacılık. Gerçek uygulama logundaki login akışı birebir tanımlı: `LoginForMobileBranch` → `InitialLogin` → `SendSmsOtp` → `OtpLoginCompleteNew`.
- **İşlemler:** Ana sayfa, Hesap listesi, Hesap hareketleri, Kur listesi.
- **Testler:** Giriş, Hesap listeleme, Hesap hareketleri görüntüleme, Kur bilgisi listeleme.
- **Yaklaşımlar:** Sınır değer analizi, Eşdeğerlik sınıfları, Negatif testler, Pairwise, Kullanıcı yolculuğu.

Sistem tanımında iki ortam var: yerel **demo** ve gerçek **prep** (`mobileprep.kuveytturk.com.tr`, logdaki cihaz ve uygulama bilgileriyle). **Şifreler ve OTP tanım dosyalarında yer almaz.** Bunlar ya ortam değişkeninden (`STM_SECRET_...`) okunur ya da yalnızca sizin bilgisayarınızdaki `gizli.yaml` dosyasından. Bu dosyayı `import_log(save_secrets=true)` kendi logunuzdan doldurur. Ek olarak kuruma özel bir tanım klasörü kullanılacaksa, zip klasörüne `kurum-tanimlari` adıyla konduğunda kurulum onu da yükler.

## Ajanın araçları (8)

| Araç | Ne yapar |
|---|---|
| `list_tests` | Tanımlı sistemleri, işlemleri (girdileriyle), testleri ve yaklaşımları listeler; tek bir tanımı sürüm bilgisiyle okur |
| `run_test` | Testi verilen müşteri numarası ve ortamla koşar: tek test, kaydedilmemiş taslak (`content`) ya da etikete göre toplu |
| `call_operation` | Tek bir işlemi Türkçe adı ve sade girdileriyle dener |
| `generate_tests` | Bir yaklaşımla, işlemin tanımından **kodla** taslak tablo testi üretir (deterministik) |
| `save_definition` | İşlem, test, yaklaşım ya da sistem tanımını doğrular ve kaydeder (sürüm kontrollü) |
| `import_log` | Uygulama logunu (ya da HAR'ı) sunucuda okur ve tanımlı olmayan işlemler için taslak önerir |
| `check_connection` | Bağlantıyı adım adım sınar (DNS → proxy → TCP → TLS → HTTP) ve nerede koptuğunu söyler |
| `end_sessions` | Oturumları kapatır ve test müşterilerini başkalarına bırakır |

Login, OTP, 5 dakikalık oturumun yenilenmesi ve zarf alanları sunucuda yapılır; ajan bunlarla uğraşmaz. Ama gizlenmez: her adım sonuçta görünür (aşağıda "Şeffaflık"). Aynı müşteriyle paralel login yapılmaz: başka bir pencere, ajan ya da CI o müşteriyi kullanıyorsa sunucu bunu söyler. Prod engeli ve denetim logu her çağrıda geçerlidir.

## Şeffaflık: kara kutu değil, cam kutu

HTTP çağrısı yapan her araç sonucunda üç alan gelir:

| Alan | İçerik |
|---|---|
| `akis` | Her HTTP alışverişi için bir satır: hangi adım (`giriş 2/4`, `test`, `çıkış`), metod, tam URL, HTTP durumu ya da hatanın aşaması, süre, iş sonucu, yanıttan alınan oturum değerleri |
| `iz` | Gönderilen isteğin tamamı (URL, oturumun eklediği başlıklar dahil tüm başlıklar, gövde, zaman aşımı, proxy, TLS ayarı) ve gelen yanıtın tamamı (durum, başlıklar, gövde, yönlendirmeler). Ajanlar uzun araç sonuçlarını kestiği için yalnızca sorunlu alışverişler (hata, HTTP ≥ 400, iş sonucu başarısız) burada gösterilir; `STM_IZ=hepsi` hepsini gösterir. Uzun gövdeler `STM_IZ_GOVDE` karakterde (varsayılan 2000) kısaltılır. |
| `iz_dosyasi` | Kısaltılmamış izin JSON dosyası: `%USERPROFILE%\.service-test-mcp\izler\` (son 200 dosya tutulur; `STM_IZ_DIR`) |

Örnek `akis`:

```
1) [giriş 1/4] POST https://.../AuthenticationService.svc/LoginForMobileBranch -> HTTP 200, 412 ms, iş sonucu: başarılı, alınan: oturum.SessionKey
2) [giriş 2/4] POST https://.../AuthenticationService.svc/InitialLogin -> HATA: bağlantı kuruldu, istek gitti; sunucu 30 sn içinde yanıt vermedi [ReadTimeout], 30004 ms
```

Hata olduğunda sonuçta hatanın türü (`tur`), aşaması (`asama`: DNS, TCP bağlantısı, TLS, proxy, yanıt beklerken ...), tam neden zinciri (`zincir`) ve olası nedenler (`olasi_nedenler`) yer alır. Sunucunun kendi kodunda beklenmeyen bir hata olursa hatanın oluştuğu dosya ve satırlar (`konum`) da döner. Zaman aşımı ya da bağlantı hatasında `check_connection` her yolu (şu anki, doğrudan, Windows/tarayıcının seçtiği proxy) adım adım sınar, hangisinin çalıştığını ve sistem tanımına yazılacak satırı söyler.

**Gizli değerler:** Varsayılan mod `STM_SEFFAF=seffaf`: yalnızca kimlik bilgileri (şifreler, OTP, `gizli.yaml` ve `STM_SECRET_...` değerleri) `«gizli: ad»` etiketiyle gösterilir; müşteri no, IBAN, SessionKey, tutarlar gibi her şey olduğu gibi görünür. `STM_GIZLILERI_GOSTER=1` kimlik bilgilerini de açık gösterir (yalnızca kendi bilgisayarınızda öğrenme amaçlı). `STM_SEFFAF=maskeli` eski davranışı (kişisel veri maskeleme) geri getirir. Denetim logu her modda maskelidir.

**Bağlantı ayarları** sistem tanımında ortam başına yazılır:

```yaml
ortamlar:
  prep:
    adres: https://mobileprep.kuveytturk.com.tr/BOA.Integration.MobileBranch
    zaman_asimi_sn: 60                       # varsayılan 30
    proxy: otomatik                          # tarayıcı gibi (Windows PAC); ya da http://adres:port; 'yok' = doğrudan
    tls_dogrula: C:\kurum\kurum-ca.pem       # true / false / kurum CA dosyası
```

Uzman kullanım için `STM_TOOLSET=uzman` ham SOAP/REST, profil ve YAML senaryo araçlarını da açar (toplam 24 araç).

## Deterministik kontrol ve AI yorumu

Kararı makine verir, yorumu AI yapar:

- **`kontrol:` satırları deterministiktir.** Sunucu bunları değerlendirir; sonuç her seferinde aynıdır ve CI'da AI olmadan koşar.
  ```
  başarılı                          başarısız                         süre < 3 sn
  AccountGroups.Accounts sayısı >= 1                                  Value.CustomerName boş değil
  AccountGroups.Accounts.IBANNumber eşleşir ^TR\d{24}$                hata kodu = INVALID_DATE_RANGE
  her FXRateModelList için CurrencyBid <= CurrencyAsk
  her AccountTransactionHistoryList için TransactionDate arasında {{baslangic}} ve {{bitis}}
  ```
  Alan yolları yanıttaki alan adlarıdır; listeler otomatik gezilir. Bakiye, kur, tarih gibi değişen veriler için sabit değer yerine **kural** yazılır.
- **`beklenen: ?` sonuçları gözlem olarak raporlanır.** Servisin ne yapması gerektiği bilinmiyorsa ajan karar vermez; davranışı size gösterir ve sorar. Onayladığınız sonuç teste yazılır ve o andan itibaren test deterministik koşar.
- **`ai_yorum:` sorularını ajan yanıtlar.** Örnek: "Hareket açıklamaları anlamlı mı?" Bu yanıt ayrı bir bölümde "AI görüşü" olarak gelir ve geçti/kaldı kararını etkilemez.

## Test örnekleri

**Adımlı test.** `hesap_no`, ilk adımın yanıtından alınır:
```yaml
test: Hesap hareketleri görüntüleme
adimlar:
  - islem: Hesap listesi
    al: { hesap_no: "AccountGroups.Accounts[0].AccountNumber", hesap_ek: "AccountGroups.Accounts[0].AccountSuffix" }
  - islem: Hesap hareketleri
    girdi: { hesap_no: "{{hesap_no}}", hesap_ek: "{{hesap_ek}}", baslangic: bugün-30 }
    kontrol:
      - her AccountTransactionHistoryList için TransactionDate arasında {{baslangic}} ve {{bitis}}
    ai_yorum: Hareket açıklamaları ve tutarları anlamlı mı?
```

**Tablo testi.** "Sınır değerle tara" isteğine `generate_tests` böyle bir tablo üretir:
```yaml
test: Hesap hareketleri - Sınır değer analizi
islem: Hesap hareketleri
ornekler:
  - { durum: aynı gün (0 gün),         baslangic: bugün,     bitis: bugün,   beklenen: başarılı }
  - { durum: bitiş başlangıçtan önce,  baslangic: bugün,     bitis: bugün-1, beklenen: hata }
  - { durum: tam 365 gün,              baslangic: bugün-365, bitis: bugün,   beklenen: başarılı }
  - { durum: 366 gün (sınır dışı),     baslangic: bugün-366, bitis: bugün,   beklenen: hata }
  - { durum: gelecek tarihli bitiş,    baslangic: bugün,     bitis: bugün+1, beklenen: "?" }
```

Değer ifadeleri: `bugün`, `bugün-7`, `bugün+1`; tarih biçimleri `GG.AA.YYYY` ve `YYYY-AA-GG`. Bir girdi `null` verilirse hiç gönderilmez (eksik alan testi). `{ham: "abc"}` biçimindeki değer tip dönüşümü yapılmadan, olduğu gibi gönderilir (yanlış tip testi).

## İşlem tanımı

```yaml
ad: Hesap hareketleri
metod: AccountTransactionHistoryList
yol: BOA.Integration.MobileBanking.Account/AccountService.svc
tur: okuma                  # okuma | degistirme | giris | bilinmiyor
girdiler:
  hesap_no:  { alan: AccountNumber, tip: sayi, zorunlu: true,
               kaynak: { islem: Hesap listesi, al: "AccountGroups.Accounts[0].AccountNumber" } }
  hesap_ek:  { alan: AccountSuffix, tip: sayi, varsayilan: 1, en_az: 1 }
  baslangic: { alan: BeginDate, tip: tarih, bicim: GG.AA.YYYY, varsayilan: bugün-7 }
  bitis:     { alan: EndDate,   tip: tarih, bicim: GG.AA.YYYY, varsayilan: bugün }
tarih_araligi: { baslangic: baslangic, bitis: bitis, en_fazla_gun: 365 }
sabit: { MainResourceCode: MOBROTHRBN, AccountType: Cari }
```

`en_az`, `en_fazla`, `secenekler` ve `tarih_araligi`, test üreticilerinin dayanağıdır. Bu bilgiler tanımda yoksa ajan size sorar ya da keşif çağrılarıyla bulup öneri olarak sunar.

## Kurulum ve VS Code

1. Python'unuza uygun zip'i (`release/archi-service-test-mcp-windows-x64.zip` ya da `-x86.zip`) bir klasöre açın.
2. Klasörü VS Code'da açın ve Copilot Chat → **Agent** moduna şunu yazın: `KURULUM_REHBERI.md dosyasını oku ve "Ajan için kurulum adımları" bölümünü sırayla uygula.`

   Ya da elle `install.cmd` çalıştırın. Kurulum yeri `%USERPROFILE%\.service-test-mcp` olur; tanımlar oraya kopyalanır.
3. `Ctrl+Shift+P` → **MCP: Open User Configuration**:
   ```json
   { "servers": { "archi-service-test": { "type": "stdio",
       "command": "${userHome}\\.service-test-mcp\\Scripts\\service-test-mcp.exe" } } }
   ```
4. Gizli değerleri yükleyin. Uygulamayı loglama modunda bir kez kullanın, sonra Copilot'a şunu yazın:
   > `C:\loglar\TransactionLog.txt logunu import_log ile oku; gizli değerleri kaydetmeme izin veriyorum (save_secrets).`

   Uygulama şifreleri, ortak test şifresi ve OTP yerel `gizli.yaml` dosyasına yazılır; hiçbir çıktıda görünmez. Alternatif olarak bunları `mcp.json` içinde `STM_SECRET_MUSTERI_SIFRESI`, `STM_SECRET_OTP`, `STM_SECRET_UYGULAMA_SIFRESI` ve `STM_SECRET_UYGULAMA_SIFRESI_2` ortam değişkenleriyle (`${input:...}`) de verebilirsiniz.
5. Deneyin: "*Kur bilgisi listeleme testini 12345678 ile prep'te çalıştır*".

Copilot custom agent ve hazır görevler: `.github/agents/service-tester.agent.md`, `.github/prompts/`. Agent dosyası, bağımsız ajanın da kullandığı tek talimat kaynağından (`src/service_test_mcp/prompts/service-tester.md`) üretilir.

## Demo (hiçbir yere bağlanmadan)

```bat
service-test-mcp --demo-server
rem baska terminalde:
service-test-mcp --init
service-test-mcp --test hepsi --musteri 1000001 --env demo
```

Demo; logdaki gerçek login akışını, hesap, hareket (tarih kurallarıyla) ve kur servislerini sentetik verilerle taklit eder.

## Bağımsız ajan ve CI

```bat
set STM_AGENT_BASE_URL=http://llm.sirket.local:8000/v1
set STM_AGENT_MODEL=kurum-modeli
service-test-agent --prompt "Hesap hareketlerini sınır değer yaklaşımıyla tara, 12345678 ile prep'te" --output rapor.md
```

Varsayılan olarak modele yalnızca 8 araç verilir; küçük modeller için de uygundur. Model sunucusunda tool calling açık olmalıdır.

AI olmadan (CI, zamanlanmış koşu):
```bat
service-test-mcp --test hepsi --tags duman --musteri 12345678 --env prep --junit sonuc.xml
```
Yalnızca **onaylı** testler koşar. Çıkış kodları: `0` hepsi geçti (gözlemler dahil), `1` kalan test var, `2` yapılandırma hatası.

## Bilinmesi gerekenler

- **Logdan yanıt alınmaz.** Bazı uygulama logları, aynı anda yapılan çağrıların yanıtlarını yanlış metoda yazıyor. Bu yüzden yanıt alanları ilk gerçek çağrıda görülür; `import_log` yalnızca istekleri ve akışı kullanır.
- **Açık konular:** Header'da taşınan değerler henüz belirlenmedi; sistem tanımında `istek.basliklar` alanı hazır bekliyor. Sunucunun oturum bittiğinde döndüğü hata kodu da öğrenilince `oturum.bitti_sayilir` alanına yazılmalı (demo `SESSION_EXPIRED` kullanıyor).
- **Ayrıntılar:** Mimari [docs/MIMARI.md](docs/MIMARI.md), yeni bir sistem eklemek [docs/YENI-SERVIS.md](docs/YENI-SERVIS.md). Uzman araç setinin profil ve senaryo biçimleri `src/service_test_mcp/profile/model.py` ile `scenario/model.py` dosyalarının başındaki açıklamalarda.

## Geliştirme

```bash
pip install -e ".[dev]"
pytest -v
python scripts/sync_agent_files.py                 # ajan talimatı değişince
pip install uv && python scripts/build_bundle.py   # release/*.zip
```

## Lisans

MIT
