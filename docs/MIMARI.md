# Mimari

## Hedef

Servis katmanı testlerini bir AI ajanının yapabildiği bir platform. Teknik bilgisi az olan kişiler de ajana "şu akışları şu müşteriyle test et" ya da "şu işlemi sınır değerle tara" diyebilmeli. İlk hedef mobil bankacılık WCF servisleri; diğer servisler aynı yapıya tanım olarak eklenir. Platform VS Code Copilot'ta ve bağımsız ajanlarda aynı şekilde çalışır.

## Kararlar

| Konu | Karar | Koddaki karşılığı |
|---|---|---|
| Kullanıcıya görünen model | Dört kavram: sistem, işlem, yaklaşım, test. Hepsi düz YAML. | `tanim/` paketi, `tanimlar/` klasörü |
| MCP yapısı | Tek MCP. Yeni servisler ve test yaklaşımları tanım dosyası olarak eklenir. | `tanim/derle.py`: sistem → servis + profil |
| Araçlar | Varsayılan 7 araç; uzman set (23 araç) isteğe bağlı. | `TEMEL_TOOL_NAMES`, `STM_TOOLSET` |
| Karar mekanizması | Kontroller deterministik, sunucuda değerlendirilir. AI yalnızca yazarken ve yorumlarken devreye girer. | `tanim/kontrol.py`, `ai_yorum` |
| Bilinmeyen beklenti | `?` sonucu gözlemdir. Kararı insan verir, sonra test deterministik koşar. | `beklenen: ?`, `onay_bekleyen` |
| Test yaklaşımları | MCP'nin içinde. Kurala dayalılar kodla üretilir; diğerleri kurallarıyla ajana verilir. | `tanim/uretec.py`, `yaklasimlar/*.yaml` |
| Müşteri | Testte yazılmaz; çalıştırırken müşteri numarası verilir. Şifre ortak ve OTP sabit (ortam başına). | `{{musteri.no}}`, `adhoc_field` |
| Paralel login | Kapalı. Süreçler ve makineler arası kilit kullanılır. | `profile/leases.py` |
| Oturum | 5 dk. Süre dolmadan yenilenir; sunucu oturumu bitirirse yeniden login ve okuma çağrısı bir kez tekrarlanır. Para hareketi tekrarlanmaz. | `oturum.sure_sn`, `bitti_sayilir` |
| Log | MCP okur, ajan özet görür. Yalnızca istekler ve akış alınır; yanıtlar alınmaz (bazı loglarda yanıtlar yanlış metoda yazılıyor). | `tanim/logdan.py` |
| Gizli değerler | Tanım dosyalarına ve repoya girmez. Ortam değişkeni ya da yerel `gizli.yaml`; logdan izinle doldurulur. | `tanim/gizli.py`, `{{gizli.x}}` |
| Ortamlar | Ortam adresi ve değişkenleri sistem tanımında; testler ortamdan bağımsız. Test ortamlarında engel yok; prod adları reddedilir. | `ortamlar:`, `PRODUCTION_NAMES` |
| Ajan talimatı | Tek kaynak; Copilot agent dosyası ondan üretilir. | `prompts/service-tester.md` |
| Arayüz | Aynı dosyaları aynı doğrulamayla düzenleyen ikinci bir istemci; JSON Schema'lar hazır. | `tanim/semalar.py`, `--schemas` |

## Katmanlar

```
   VS Code Copilot          bağımsız ajan (kurum içi LLM)          CI (--test, AI'sız)          (ileride) arayüz
          \                          |                                  |                          /
           +------------- MCP araçları: list_tests, run_test, call_operation, generate_tests, -----+
                                    save_definition, import_log, end_sessions
                                                   |
  ┌─────────────────────────────── tanım katmanı (tanim/) ───────────────────────────────────────────┐
  │ depo: yükle, ada göre bul, doğrula (şema + anlam), sürüm kontrollü kaydet                        │
  │ derle: sistem → servis + profil, işlemler → katalog     kontrol: deterministik kontrol dili      │
  │ kosucu: işlem çağrısı (varsayılan, tarih, kaynak), adımlı ve tablo testleri, gözlem, ai_yorum     │
  │ uretec: sınır değer, eşdeğerlik, negatif, pairwise      logdan: log → işlem taslakları, gizliler │
  └──────────────────────────────────────────────────────────────────────────────────────────────────┘
  ┌──────────────── profil motoru (profile/) ─────────────────┐  ┌──── çekirdek ────────────────────────┐
  │ zarf, login adımları, oturum yenileme, iş başarısı,       │  │ REST/SOAP, host allowlist, maskeleme, │
  │ müşteri kilidi, gizli değer çözümü                        │  │ denetim logu, şablonlar               │
  └───────────────────────────────────────────────────────────┘  └───────────────────────────────────────┘
```

Çekirdekte ve motorda mobil bankacılığa özgü hiçbir şey yoktur. Ona özgü her şey (zarf alanları, login adımları, `Success` kuralı, ekran kodları) `sistemler/mobil-bankacilik.yaml` ve `islemler/*.yaml` dosyalarındadır.

## Akışlar

**Test çalıştırma** (`run_test("Hesap hareketleri görüntüleme", customer="12345678", environment="prep")`):

1. Test ve işlemleri yüklenir ve doğrulanır.
2. Her adım için girdiler hazırlanır:
   - verilen değerler, yoksa varsayılanlar,
   - `bugün-30` gibi ifadeler doğru biçime çevrilir (`GG.AA.YYYY`),
   - verilmeyen zorunlu girdi `kaynak` işlemden alınır,
   - sabit alanlar eklenir.
3. Profil müşteri kilidini alır. Oturum yoksa login adımlarını koşar: uygulama girişi, müşteri no + ortak şifre, SMS OTP, sabit OTP. Ardından zarfı ekler ve isteği gönderir.
4. Kontroller değerlendirilir:
   - beklenen `başarılı` ise iş başarısı ve kontrol satırları,
   - beklenen `hata` ise iş başarısızlığı,
   - beklenen `?` ise yalnızca gözlem kaydedilir.
5. Rapor dönülür: geçti, kaldı (kalan kontrol, beklenen ve gerçek değer, maskeli yanıt özeti), gözlem, hata. `ai_yorum` soruları ve veri özeti ajana ayrı bölümde gelir.

**Yaklaşımla tarama** ("hesap hareketlerini sınır değerle tara"):

1. `generate_tests("sınır değer", "Hesap hareketleri")`. Yaklaşım, takma adlarıyla da bulunur ("boundary value").
2. Üretici işlem tanımındaki `en_az`, `en_fazla` ve `tarih_araligi` bilgisinden tabloyu kurar. Eksik bilgi varsa `notlar` alanında söyler.
3. Ajan taslağı `run_test(content=...)` ile koşar. Gözlemleri size sorar, onay gelirse `save_definition` ile kaydeder.

**Logdan tanım** (`import_log(path)`):

1. Log sunucuda okunur ve istekler/yanıtlar eşleştirilir.
2. URL'nin başı sistemlerin ortam adresleriyle eşleştirilir; ortam da buradan bulunur.
3. Gürültü (bildirim, client log, kampanya çağrıları) ayıklanır.
4. Tanımlı olmayan her işlem için bir taslak çıkarılır:
   - zarf, kimlik ve müşteri alanları ayrılır,
   - tarihler log gününe göre `bugün-7` gibi göreli ifadeye çevrilir,
   - bayraklar ve ekran kodları sabit alana gider.
5. `apply=true` taslakları kaydeder. `save_secrets=true`, sistem tanımındaki `gizli_degerler` listesine göre şifreleri ve OTP'yi yerel `gizli.yaml`'a yazar.

**Doğrulama:** Bu tanımlar gönderilen gerçek logla karşılaştırıldı. Motorun ürettiği istekler (4 login adımı ve 4 işlem; toplam 206 alan, gizli değerler dahil) uygulamanın gönderdikleriyle birebir aynı. Canlı servise henüz bağlanılmadı.

## Deterministik ve AI dengesi

| | Deterministik (kontrol, tablo beklentisi) | AI (taslak, yorum) |
|---|---|---|
| Nerede | Koşu anında, sunucuda | Test yazılırken; `ai_yorum` sorularında; başarısızlığı açıklarken |
| Artı | Tekrarlanabilir, CI'da AI'sız, ucuz | Kural yazılamayan şeyleri görür; Türkçe isteği teste çevirir |
| Eksi | Öngörülmeyeni yakalamaz | Tutarsız olabilir |
| Kural | Geçti/kaldı kararını yalnızca bu verir | Karar vermez; bilinmeyen beklentiyi insana sorar |

## Genişleme noktaları

| Ne | Nasıl |
|---|---|
| Yeni sistem | `sistemler/` altına bir dosya ([YENI-SERVIS.md](YENI-SERVIS.md)) |
| Yeni işlem | Log ile `import_log`, ya da `save_definition` |
| Yeni yaklaşım | `yaklasimlar/` altına bir dosya. Kodla üretim gerekiyorsa `uretec.py` içine bir üretici |
| YAML'ın yetmediği mantık | Profil hook'u (`prepare` / `inspect`) |
| Yeni log biçimi | `importer/parsers.py` içine bir fonksiyon |
| Ortak kilit deposu | `LeaseStore` arayüzü (DB, Redis) |
| Arayüz | Aynı depo ve doğrulama üzerinden bir HTTP API ve formlar (şemalar hazır) |

## Açık konular

1. **Header'lar:** Header'da taşınan değerler belirlenince `istek.basliklar` alanına yazılacak.
2. **Oturum bitiş kodu:** Gerçek servisin oturum bittiğinde döndüğü hata kodu öğrenilince `oturum.bitti_sayilir` güncellenecek.
3. **Hareket alanları:** Hesap hareketi yanıtındaki alan adları (`TransactionDate`, `Amount`) logda örnek bulunmadığı için doğrulanmadı; ilk gerçek çağrıda kontrol edilmeli.
4. **Ortak müşteri kullanımı:** Ekip ve CI aynı müşterileri farklı makinelerden kullanacaksa `locks.dir` ortak bir klasöre alınacak.
5. **Arayüz:** Tanımlar gerçek servisle doğrulandıktan sonra eklenecek.
