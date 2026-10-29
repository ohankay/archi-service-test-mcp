# Mimari

## Hedef

Servis katmanı testlerini bir AI ajanının yapabildiği bir platform. Teknik bilgisi az olan kişiler de ajana "şu akışları şu müşteriyle test et" ya da "şu işlemi sınır değerle tara" diyebilmeli. İlk hedef mobil bankacılık WCF servisleri; diğer servisler aynı yapıya tanım olarak eklenir. Platform VS Code Copilot'ta ve bağımsız ajanlarda aynı şekilde çalışır.

## Kararlar

| Konu | Karar | Koddaki karşılığı |
|---|---|---|
| Adlandırma | Kod, tanım anahtarları, araç sonuçları, ayarlar, dosya ve klasör adları İngilizce. Test edilen servislerin alan adları ve mesajları olduğu gibi kalır; kullanıcı ajanla kendi dilinde konuşur. | `definitions/`, `definitions/migrate.py` |
| Kullanıcıya görünen model | Dört kavram: system, operation, approach, test. Hepsi düz YAML. | `definitions/` paketi ve klasörü |
| MCP yapısı | Tek MCP. Yeni servisler ve test yaklaşımları tanım dosyası olarak eklenir. | `definitions/compiler.py`: system → servis + profil |
| Araçlar | Varsayılan 8 araç (`basic`); uzman set (`expert`, 24 araç) isteğe bağlı. | `BASIC_TOOL_NAMES`, `STM_TOOLSET` |
| Karar mekanizması | Kontroller deterministik, sunucuda değerlendirilir. AI yalnızca yazarken ve yorumlarken devreye girer. | `definitions/checks.py`, `ai_review` |
| Bilinmeyen beklenti | `?` sonucu gözlemdir (`observation`). Kararı insan verir, sonra test deterministik koşar. | `expected: ?`, `pending_review` |
| Test yaklaşımları | MCP'nin içinde. Kurala dayalılar kodla üretilir; diğerleri kurallarıyla ajana verilir. | `definitions/generators.py`, `approaches/*.yaml` |
| Müşteri | Testte yazılmaz; çalıştırırken müşteri numarası verilir. Şifre ortak ve OTP sabit (ortam başına). | `{{customer.no}}`, `adhoc_field` |
| Paralel login | Kapalı. Süreçler ve makineler arası kilit kullanılır. | `profile/leases.py` |
| Oturum | 5 dk. Süre dolmadan yenilenir; sunucu oturumu bitirirse yeniden login ve okuma çağrısı bir kez tekrarlanır. Para hareketi tekrarlanmaz. | `session.ttl_s`, `session.expired_when` |
| Log | MCP okur, ajan özet görür. Yalnızca istekler ve akış alınır; yanıtlar alınmaz (bazı loglarda yanıtlar yanlış metoda yazılıyor). | `definitions/log_import.py` |
| Gizli değerler | Tanım dosyalarına ve repoya girmez. Ortam değişkeni ya da yerel `secrets.yaml`; logdan izinle doldurulur. | `definitions/secrets.py`, `{{secret.x}}` |
| Ortamlar | Ortam adresi, bağlantı ayarları ve değişkenleri sistem tanımında; testler ortamdan bağımsız. Test ortamlarında engel yok; prod adları reddedilir. | `environments:`, `PRODUCTION_NAMES` |
| Şeffaflık | Cam kutu: her araç sonucu `result` ve `problem` ile başlar, yaptığı HTTP alışverişlerini (`flow`, `trace`) ve tam iz dosyasını (`trace_file`) döner; hatalar aşaması, neden zinciri ve olası nedenleriyle gelir. Yalnızca kimlik bilgileri etiketlenir. Sonuç 8000 karakteri geçmez. | `trace.py`, `connection.py`, `STM_TRANSPARENCY` |
| Ajan talimatı | Tek kaynak; Copilot agent dosyası ondan üretilir. | `prompts/service-tester.md` |
| Geçiş | Önceki sürümün Türkçe tanımları (`tanimlar/`, `gizli.yaml`) ilk açılışta bir kez çevrilir; eski ayar adları da okunur. | `definitions/migrate.py`, `config.env_setting` |
| Arayüz | Aynı dosyaları aynı doğrulamayla düzenleyen ikinci bir istemci; JSON Schema'lar hazır. | `definitions/schemas.py`, `--schemas` |

## Katmanlar

```
   VS Code Copilot          bağımsız ajan (kurum içi LLM)          CI (--test, AI'sız)          (ileride) arayüz
          \                          |                                  |                          /
           +------------- MCP araçları: list_tests, run_test, call_operation, generate_tests, -----+
                         save_definition, import_log, check_connection, end_sessions
                         (her sonuçta: result, problem, flow, trace, trace_file)
                                                   |
  ┌─────────────────────────────── tanım katmanı (definitions/) ─────────────────────────────────────┐
  │ store: yükle, ada göre bul, doğrula (şema + anlam), sürüm kontrollü kaydet                       │
  │ compiler: system → servis + profil, operations → katalog     checks: deterministik kontrol dili  │
  │ runner: işlem çağrısı (varsayılan, tarih, kaynak), adımlı ve tablo testleri, gözlem, ai_review    │
  │ generators: boundary value, equivalence, negative, pairwise   log_import: log → işlem taslakları │
  │ migrate: önceki sürümün tanımları → bu sürüm                                                      │
  └──────────────────────────────────────────────────────────────────────────────────────────────────┘
  ┌──────────────── profil motoru (profile/) ─────────────────┐  ┌──── çekirdek ────────────────────────┐
  │ zarf, login adımları, oturum yenileme, iş başarısı,       │  │ REST/SOAP, host allowlist, maskeleme, │
  │ müşteri kilidi, gizli değer çözümü                        │  │ iz (trace), bağlantı teşhisi,         │
  │                                                           │  │ denetim logu, şablonlar               │
  └───────────────────────────────────────────────────────────┘  └───────────────────────────────────────┘
```

Çekirdekte ve motorda mobil bankacılığa özgü hiçbir şey yoktur. Ona özgü her şey (zarf alanları, login adımları, `Success` kuralı, ekran kodları) `systems/mobile-banking.yaml` ve `operations/*.yaml` dosyalarındadır.

## Akışlar

**Test çalıştırma** (`run_test("Account transactions view", customer="12345678", environment="prep")`):

1. Test ve işlemleri yüklenir ve doğrulanır.
2. Her adım için girdiler hazırlanır:
   - verilen değerler, yoksa varsayılanlar (`default`),
   - `today-30` gibi ifadeler doğru biçime çevrilir (`DD.MM.YYYY`),
   - verilmeyen zorunlu girdi `source` işlemden alınır,
   - sabit alanlar (`fixed`) eklenir.
3. Profil müşteri kilidini alır. Oturum yoksa login adımlarını koşar: uygulama girişi, müşteri no + ortak şifre, SMS OTP, sabit OTP. Ardından zarfı ekler ve isteği gönderir.
4. Kontroller değerlendirilir:
   - beklenen `success` ise iş başarısı ve kontrol satırları,
   - beklenen `error` ise iş başarısızlığı,
   - beklenen `?` ise yalnızca gözlem kaydedilir.
5. Rapor dönülür: `passed`, `failed` (kalan kontrol, beklenen ve gerçek değer, yanıt özeti), `observation`, `error`. Login dahil her HTTP alışverişi `flow` satırı olarak, sorunlu olanlar `trace` içinde gelir; tam iz `trace_file`'dadır. `ai_review` soruları ve veri özeti ajana ayrı bölümde gelir.

**Yaklaşımla tarama** ("hesap hareketlerini sınır değerle tara"):

1. `generate_tests("boundary value", "Account transactions")`. Yaklaşım, takma adlarıyla (`aliases`, "sınır değer" dahil) de bulunur.
2. Üretici işlem tanımındaki `min`, `max` ve `date_range` bilgisinden tabloyu kurar. Eksik bilgi varsa `notes` alanında söyler.
3. Ajan taslağı `run_test(content=...)` ile koşar. Gözlemleri size sorar, onay gelirse `save_definition` ile kaydeder.

**Logdan tanım** (`import_log(path)`):

1. Log sunucuda okunur ve istekler/yanıtlar eşleştirilir.
2. URL'nin başı sistemlerin ortam adresleriyle eşleştirilir; ortam da buradan bulunur.
3. Gürültü (bildirim, client log, kampanya çağrıları) ayıklanır.
4. Tanımlı olmayan her işlem için bir taslak çıkarılır:
   - zarf, kimlik ve müşteri alanları ayrılır,
   - tarihler log gününe göre `today-7` gibi göreli ifadeye çevrilir,
   - bayraklar ve ekran kodları sabit alana (`fixed`) gider.
5. `apply=true` taslakları kaydeder. `save_secrets=true`, sistem tanımındaki `secret_values` listesine göre şifreleri ve OTP'yi yerel `secrets.yaml`'a yazar.

**Bağlantı teşhisi** (`check_connection("Mobile Banking", "prep")`): şu anki yol, doğrudan bağlantı ve Windows'un (tarayıcının) bu adres için seçtiği proxy ayrı ayrı denenir; her yol için DNS, TCP, TLS ve tek bir HTTP isteği süreleriyle raporlanır. Çalışan yol için sistem tanımına yazılacak satır `suggestion` alanında gelir. Kaydedilen bağlantı ayarları yeniden başlatmadan geçerli olur.

**Doğrulama:** Bu tanımlar (prep ortamı) gönderilen gerçek logla karşılaştırıldı; motorun ürettiği istekler (4 login adımı ve 4 işlem; toplam 206 alan) uygulamanın gönderdikleriyle birebir aynı. Prep ortamında login ve hesap listesi gerçek servisle çalıştı.

## Deterministik ve AI dengesi

| | Deterministik (kontrol, tablo beklentisi) | AI (taslak, yorum) |
|---|---|---|
| Nerede | Koşu anında, sunucuda | Test yazılırken; `ai_review` sorularında; başarısızlığı açıklarken |
| Artı | Tekrarlanabilir, CI'da AI'sız, ucuz | Kural yazılamayan şeyleri görür; kullanıcının isteğini teste çevirir |
| Eksi | Öngörülmeyeni yakalamaz | Tutarsız olabilir |
| Kural | passed/failed kararını yalnızca bu verir | Karar vermez; bilinmeyen beklentiyi insana sorar |

## Genişleme noktaları

| Ne | Nasıl |
|---|---|
| Yeni sistem | `systems/` altına bir dosya ([NEW-SYSTEM.md](NEW-SYSTEM.md)) |
| Yeni işlem | Log ile `import_log`, ya da `save_definition` |
| Yeni yaklaşım | `approaches/` altına bir dosya. Kodla üretim gerekiyorsa `generators.py` içine bir üretici |
| YAML'ın yetmediği mantık | Sistem hook'u (`hook: file.py:Class`, `prepare` / `inspect`) |
| Yeni log biçimi | `importer/parsers.py` içine bir fonksiyon |
| Ortak kilit deposu | `LeaseStore` arayüzü (DB, Redis) |
| Arayüz | Aynı depo ve doğrulama üzerinden bir HTTP API ve formlar (şemalar hazır) |

## Açık konular

1. **Header'lar:** Header'da taşınan değerler belirlenince `request.headers` alanına yazılacak.
2. **Oturum bitiş kodu:** Gerçek servisin oturum bittiğinde döndüğü hata kodu öğrenilince `session.expired_when` güncellenecek.
3. **Hareket alanları:** Hesap hareketi yanıtındaki alan adları (`TransactionDate`, `Amount`) logda örnek bulunmadığı için doğrulanmadı; ilk gerçek çağrıda kontrol edilmeli.
4. **Ortak müşteri kullanımı:** Ekip ve CI aynı müşterileri farklı makinelerden kullanacaksa `locks.dir` ortak bir klasöre alınacak.
5. **Arayüz:** Tanımlar gerçek servisle doğrulandıktan sonra eklenecek.
