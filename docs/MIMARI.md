# Mimari

Bu doküman, beyin fırtınasında verilen kararları ve bu kararların koddaki karşılıklarını özetler.

## Hedef

Mobil bankacılık WCF servislerinin **servis katmanı testlerini bir AI ajanının yapabildiği** bir platform kurmak. Platform zamanla başka servisleri de (mobil bankacılıkla ilgisi olmayanlar dahil) aynı şekilde test edebilmeli.

## Kararlar

| Konu | Karar | Koddaki karşılığı |
|---|---|---|
| MCP yapısı | Tek MCP. Mobil WCF ilk servis profili. | `profile/` paketi; `services.yaml` içinde `profile:` |
| Yeni servisler | Aynı kod tabanında yeni bir profil olarak eklenir. YAML'ın ifade edemediği mantık profil hook'una yazılır. Ekip veya güvenlik ayrımı gerekirse aynı kodun ayrı bir instance'ı açılır. | `hooks: dosya.py:Sinif`; `STM_CONFIG` ile ayrı instance |
| Katalog | Logdan üretilir, elle bakımı yapılır. | `importer/`, `import_traffic_log`, `*.catalog.yaml` |
| Log işleme | Logu MCP okur, parse eder ve maskeler. Ajan yalnızca özetle çalışır. Katalog kullanıcı onayıyla yazılır. | `import_traffic_log(apply=false)` önizleme, `apply=true` yazma |
| Log ortamı | Ortam host'tan çıkarılır (test/prep). Tanınmayan host'ta tahmin yürütülmez. | `Importer._locate` |
| Senaryo | Logdaki akıştan taslak senaryo üretilebilir. | `scenario_path`; `_` ile başlayan taslaklar suite'te atlanır |
| Login | Otomatik; OTP ortam bazlı sabit değer. | `session.login` adımları, `{{env.otp}}` |
| Session | 5 dk. Login ihtiyaç anında yapılır. Önleyici yenileme (yaklaşık 4,5 dk) ve tepkisel yenileme (sunucu session'ı bitirirse) var. Para hareketlerinde otomatik tekrar yapılmaz. | `session.ttl_s/refresh_margin_s/expired_when/retry_mutating_on_expired` |
| Test müşterisi | Persona: sabit, havuz ya da API'den alınan. | `environments.<env>.personas` |
| Paralel login | Kapalı. Süreçler ve makineler arası persona kilidi (süreli lease) kullanılır. | `profile/leases.py` (`FileLeaseStore`), `locks.dir` |
| Engeller | Test ortamlarında yok (para transferi dahil). Prod adları ve host allowlist korunur. | `allow_mutating: true`, `PRODUCTION_NAMES`, `check_host` |
| Çalışma modu | VS Code + Copilot ve bağımsız ajan, ortak profil ve senaryolarla. CI için AI'sız koşu. | stdio / HTTP MCP, `service-test-agent`, `--run` |
| Model | Kurum içi LLM (henüz belli değil). Tasarım zayıf modele göre yapıldı. | `--toolset profile`, kısa çıktılar, ağır iş sunucuda |
| Ortamlar | dev/test/prep. URL, OTP, persona ve test verisi ortam bazlı. | `base_url` ortam bazlı; profil `environments` |
| Ajan talimatı | Tek kaynak. | `prompts/service-tester.md` → `scripts/sync_agent_files.py` |

## Katmanlar

```
                    VS Code + Copilot (stdio)     Bağımsız ajan (kurum içi LLM)     CI (--run, AI'sız)
                               \                           |                              /
                                +-------------- MCP araçları (server.py) ---------------+
                                                           |
   ┌───────────────────────────────────────────────────────┴──────────────────────────────────────────────┐
   │ Profil katmanı (profile/)            │ Bilgi katmanı (importer/)        │ Senaryo katmanı (scenario/)    │
   │  - model: profil, katalog, persona   │  - parsers: TransactionLog, HAR  │  - YAML model ve doğrulama     │
   │  - runtime: zarf, login, session,    │  - analyze: servis/ortam bulma,  │  - runner: adımlar, extract,   │
   │    yenileme, iş başarısı, hook       │    alan sınıfları, bağımlılık,   │    persona, JUnit              │
   │  - leases: persona kilidi            │    şema, diff, taslak senaryo    │  - asserts: deterministik      │
   ├──────────────────────────────────────┴──────────────────────────────────┴───────────────────────────────┤
   │ Çekirdek (değişmedi): REST/SOAP adaptörleri, host allowlist, maskeleme, denetim logu, şablonlar           │
   └──────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Çekirdekte mobil bankacılığa özgü hiçbir şey yoktur. Zarf alanları, login adımları, `$.Success` kuralı, persona alanları ve gürültü listesi profil dosyasındadır. İkinci bir servis ailesi eklendiğinde çekirdeğe dokunulmaz.

## Akışlar

### Bir operasyon çağrısı (`call_operation`)

```
operasyon katalogda var mı? ──hayır──> hata: list_operations / import_traffic_log öner
  │ evet
mutating ve ortam izin vermiyor mu? ──evet──> hata (kind: unknown da mutating sayılır)
  │
session gerekiyor mu? ──hayır──> varsa canlı session'ın değerleriyle gönder
  │ evet
persona seç (parametre / profil varsayılanı / tek persona)
  │
session var ve canlı mı? ── kilit yenilenemedi ──> session düşer, persona yeniden alınır
  │                       ── süre doluyor (~4,5 dk) ──> aynı müşteriyle yeniden login
  │ yok
persona kilidi al (sabit / havuzdan boş olan / API'den alınan) ──alınamadı──> wait_s kadar bekle → PersonaBusy
  │
login adımları (extract → session.*)
  │
istek = zarf + (persona varsa session_envelope) + params; header şablonları; hook.prepare
  │
gönder (allowlist, maskeleme, denetim logu) → iş başarısı (success kuralı, error_paths)
  │
expired_when eşleşti mi? ──evet──> okuma: yeniden login + bir kez tekrar (session_renewed)
                                   mutating: tekrarlama (session_expired notu); retry_mutating_on_expired ile açılabilir
```

### Persona kilidi

- Kilit anahtarı `servis|ortam|CustomerId=...`, kimlik alanı profildeki `personas.identity` ile belirlenir.
- Kilit kaydı ortak klasördeki bir JSON dosyasıdır ve sahibini (`kullanıcı@makine:pid:rastgele`), personayı ve bitiş zamanını içerir.
- Yazmalar `O_EXCL` ile oluşturulan bir mutex dosyasıyla sıraya girer; bu yöntem SMB ve NFS paylaşımlarında da çalışır.
- Her çağrıda kilit uzatılır. Süreç düzgün kapanırsa logout yapılır ve kilit bırakılır; çökerse kilit süresi dolunca düşer.
- Aynı süreçte aynı müşteri iki farklı persona adıyla kullanılmak istenirse bu da reddedilir.
- `LeaseStore` bir arayüzdür. DB veya Redis implementasyonu, profillere ve senaryolara dokunmadan eklenebilir.

### Log importu

1. **Parse:** Format otomatik algılanır. İstek ve yanıtlar eşleştirilir; yanıtı olmayan ya da yanıtı iki kez loglanmış istekler tolere edilir.
2. **Servis ve ortam:** URL, profil servislerinin ortam bazlı `base_url` değerleriyle eşleştirilir. Profil dışı kalan çağrılar (ör. chatbot API'si) ayrıca raporlanır.
3. **Gürültü:** Profildeki `noise` desenleriyle eşleşen çağrılar atlanır (polling, client log).
4. **Alan ayrımı:** İstek gövdesi zarf ve parametre olarak ayrılır. Alanlar dört sınıfa ayrılır: kimlik bilgisi (hiçbir yere yazılmaz, değeri maskeleyiciye eklenir), persona alanı (katalog örneğinde `{{persona.X}}` olur), önceki bir yanıttan gelen değer (bağımlılık) ve kullanıcı girdisi.
5. **Şema:** İstek ve yanıt şemaları çıkarılır ve mevcut katalogla birleştirilir. Diff'te yalnızca yeni alanlar ve tip değişiklikleri raporlanır; bir alanın logda görünmemesi silindiği anlamına gelmez.
6. **Tür ve session:** Tür addan tahmin edilir (`Get*` → read, `Execute*`/`Insert*` → mutating, login adımları → auth; tahmin edilemezse unknown ve kullanıcıya sorulur). Session ihtiyacı, isteğin bir login yanıtından gelen değeri (ör. session key) taşıyıp taşımadığına göre belirlenir.
7. **Onay:** `apply=false` önizlemedir. `apply=true` kataloğu yazar; elle yazılmış açıklama, tür, etiket ve session alanları korunur.

Gönderdiğiniz gerçek TransactionLog ile doğrulandı: 22 istek, 13 operasyon. Chatbot API'si profil dışı olarak ayrıldı. Rapora hiçbir şifre, token ya da session değeri sızmadı.

## Genişleme noktaları

| Ne | Nasıl |
|---|---|
| Yeni servis ailesi | Yeni profil dosyası ve katalog ([YENI-SERVIS.md](YENI-SERVIS.md)) |
| YAML'ın ifade edemediği mantık (imza, şifreleme, özel header) | `hooks: hooks.py:Sinif`, `prepare(request, ctx)` / `inspect(result, ctx)` |
| Yeni log formatı | `importer/parsers.py` içinde `PARSERS` sözlüğüne bir fonksiyon |
| Paylaşılan kilit deposu | `LeaseStore` arayüzünü implemente eden bir sınıf |
| DB doğrulama, test verisi hazırlama | Ayrı MCP'ler (sql-mcp, mongo-mcp) ile işbirliği; ajan ikisini birlikte kullanır |

## Açık konular

1. **Header'lar:** Header'da taşınan değerler belirtilecek. `request.headers` şablonu hazır ve session/persona/ortam değişkenlerini kullanabilir.
2. **Session sonu yanıtı:** Gerçek servisin session bittiğinde döndüğü hata kodu ya da mesajı öğrenilince `session.expired_when` güncellenecek.
3. **Test müşterilerinin paylaşımı:** Ekip ve CI aynı müşterileri farklı makinelerden kullanacaksa `locks.dir` ortak klasöre alınacak ya da paylaşılan bir kilit deposu eklenecek.
4. **Gerçek profil:** Gerçek ortamlar için `base_url`, zarf alanları (`KcStatus`, `ThemeId`, `OperatorName`, `NetworkConnectionType`, `MainResourceCode` importer tarafından zarf adayı olarak önerildi), persona alanları ve şifrelerin `{{secret.…}}` ile verilmesi gerekiyor. Örnek profil sentetik demo içindir.
