# archi-service-test-mcp: Kurulum Rehberi (GitHub Copilot ajanı için)

## Kullanıcı için (1 dakika)

1. Python'unuza uygun zip'i indirin ve bir klasöre açın, örneğin `C:\Araclar\archi-service-test-mcp`:
   - **64-bit Python** (çoğu bilgisayar): `archi-service-test-mcp-windows-x64.zip`
   - **32-bit Python**: `archi-service-test-mcp-windows-x86.zip`

   Hangisi olduğunu bilmiyorsanız: `py -c "import struct; print(struct.calcsize('P')*8)"` komutu `64` veya `32` yazar. Yanlış zip'i seçerseniz kurulum bunu söyler.
2. VS Code'da **File → Open Folder** ile bu klasörü açın.
3. Copilot Chat'i açın, modu **Agent** yapın ve şunu yazın:

   > `KURULUM_REHBERI.md dosyasını oku ve "Ajan için kurulum adımları" bölümünü sırayla uygula.`

4. Ajan size servislerinizi ve kimlik doğrulama tipini soracak, kurulumu kendisi yapacak.
   **Şifre veya token'ı sohbete asla yazmayın.** VS Code bunları ilk başlatmada güvenli bir kutuda soracak.

Gereksinimler: Windows 10/11 (64 veya 32 bit), Python 3.10–3.14. İnternet gerekmez, tüm paketler `wheelhouse` klasöründe.

Not: Kurulum sırasında terminalde bir kez `install.cmd` çalışır. Kurulumdan sonra servis çağrıları için PowerShell hiç kullanılmaz.

---

## Ajan için kurulum adımları

> Sen bu kurulumu yapan GitHub Copilot ajanısın. Adımları **sırayla** uygula. Her adımın sonucunu kullanıcıya kısa bir cümleyle bildir. Bir adım başarısız olursa sonraki adıma geçme; "Sorun giderme" bölümüne bak, çözemiyorsan kullanıcıya hatayı olduğu gibi göster.
>
> **Kesin kurallar:**
> - Kullanıcıdan şifre veya token İSTEME; bunları hiçbir dosyaya, komuta, ortam değişkenine veya `.env` dosyasına YAZMA. Gizli değerler yalnızca `mcp.json` içindeki `${input:...}` girdileriyle VS Code tarafından sorulur.
> - `services.yaml` içine şifre/token YAZMA; sadece `password_env` / `token_env` / `value_env` ile değişken ADI yazılır.
> - Prod ortam TANIMLAMA (prod, production, prd, canli, live adları zaten reddedilir).
> - Kullanıcının mevcut `mcp.json` dosyasındaki diğer sunuculara ve girdilere DOKUNMA; sadece `archi-service-test` sunucusunu ve onun `inputs` girdilerini ekle/güncelle.
> - Paketleri elle `pip install` ile KURMA; kurulum yalnızca `install.cmd` ile yapılır.
> - Komutları VS Code'un varsayılan terminalinde (PowerShell) çalıştır.

### Adım 1: Paketleri kur

Bu rehberin bulunduğu klasörde (zip'in açıldığı kök klasör) çalıştır:

```powershell
cmd /c install.cmd; "EXIT=$LASTEXITCODE"
```

- Çıktıda `KURULUM TAMAM` ve `EXIT=0` görmelisin.
- Kurulum yeri: `%USERPROFILE%\.service-test-mcp`. Komutlar: `Scripts\service-test-mcp.exe` (MCP sunucusu) ve `Scripts\service-test-agent.exe` (bağımsız ajan).
- Hata kodları: `10` = uygun Python yok, `11` = venv oluşturulamadı, `12` = paket kurulamadı, `13` = komut çalışmadı, `14` = zip ile Python'un mimarisi farklı (diğer zip gerekiyor), `15` = mevcut kurulum farklı mimaride, `16` = kurulum sonrası doğrulama başarısız.

### Adım 2: Kullanıcıya demo mu, gerçek servisler mi diye sor

- **Demo:** Adım 3'ü atla, Adım 4'te `templates/mcp.demo.json` şablonunu kullan; `DEMO_KLASORU` yerine bu klasördeki `demo` klasörünün tam yolunu yaz (mobil WCF demosu için `mobile` klasörünün yolunu; `STM_CONFIG` o klasördeki `services.yaml` olur). Kullanıcıya demo servisini ayrı bir terminalde başlatmasını söyle: `& "$env:USERPROFILE\.service-test-mcp\Scripts\service-test-mcp.exe" --demo-server`
- **Gerçek servisler:** Adım 3'e geç.

### Adım 3: Servis kataloğunu oluştur

Kullanıcıya şu soruları tek mesajda sor (şifre SORMA):

1. Ortam adları (örn. `test`, `preprod`) ve varsayılan ortam.
2. Her servis için: adı, tipi (`soap` / `rest`), ortam bazında WSDL adresi (SOAP) veya base URL (REST), varsa OpenAPI/Swagger adresi.
3. Kimlik doğrulama: `windows` (Windows oturumu, şifresiz - WCF'de en yaygın), `ntlm` (servis kullanıcısı + şifre), `basic`, `bearer` (token), `api_key`, `ws_username_token` (WS-Security), `client_cert`, `none`.
4. Veri değiştiren (mutating) operasyonlar: SOAP'ta operasyon adları (`TransferYap`, `*Sil`), REST'te `METHOD /yol` kalıpları.
5. Kurumsal proxy veya kendi CA sertifikası gerekiyor mu?

6. Mobil bankacılık WCF servisleri test edilecek mi? Bunun için ek bir şey yazmana gerek yok: kurulum `tanimlar` klasörünü hazır tanımlarla (demo ve prep ortamları, işlemler, yaklaşımlar, testler) doldurdu. Zip klasöründe ek olarak `kurum-tanimlari` klasörü varsa o da yüklendi.

Sonra `templates/services.yaml` dosyasını `%USERPROFILE%\.service-test-mcp\services.yaml` olarak kopyala ve kullanıcının cevaplarıyla doldur. Bu adım yalnızca SOAP/REST servisleri (1-5. sorular) için gerekir; mobil servisler tanım dosyalarından gelir. Şifreli her servis için `password_env: STM_PW_<SERVIS>` / `token_env: STM_TOKEN_<SERVIS>` gibi bir değişken adı ver.

Mobil servislerin gizli değerleri (uygulama şifreleri, ortak test şifresi, sabit OTP) hiçbir dosyaya elle yazılmaz. Kurulum bittikten sonra kullanıcı uygulamayı loglama modunda bir kez kullanır ve Copilot'a şunu yazar: "*<log dosyası> logunu import_log ile oku, gizli değerleri kaydetmeme izin veriyorum (save_secrets)*". Değerler yalnızca bu bilgisayardaki `gizli.yaml` dosyasına yazılır.

Bağlantıyı test et (şifresiz tipler için; şifreli tiplerde Adım 6'ya bırak):

```powershell
$env:STM_CONFIG = "$env:USERPROFILE\.service-test-mcp\services.yaml"
& "$env:USERPROFILE\.service-test-mcp\Scripts\service-test-mcp.exe" --check; "EXIT=$LASTEXITCODE"
Remove-Item Env:STM_CONFIG
```

`EXIT=0` ise her servis `ok: true` görünür. Değilse `error` mesajını kullanıcıya göster ve "Sorun giderme"ye bak.

### Adım 4: VS Code MCP yapılandırmasını yaz

Hedef dosya, **kullanıcı düzeyi**: `"$env:APPDATA\Code\User\mcp.json"` (Insiders: `$env:APPDATA\Code - Insiders\User\mcp.json`).

| Durum | Şablon |
|---|---|
| Demo | `templates/mcp.demo.json` |
| Tüm servisler `windows` / `none` | `templates/mcp.windows-auth.json` |
| Şifre / token gereken servis var | `templates/mcp.secrets.json` |

Kurallar:

- `services.yaml` içindeki **her** `*_env` adı için iki şey ekle: `inputs` dizisine bir `promptString` girdisi (`"password": true`) ve `archi-service-test` sunucusunun `env` bölümüne `"STM_PW_X": "${input:stm-pw-x}"` satırı. Adlar **harfi harfine aynı** olmalı. Şablondaki örnek girdilerden kullanılmayanları sil.
- Senaryolarda veya profilde gizli değer gerekiyorsa adı `STM_SECRET_` ile başlayan bir değişken ekle (`{{secret.STM_SECRET_X}}`); örneğin mobil profilin `STM_SECRET_MOBIL_APP_PASSWORD` değişkeni.
- **Dosya yoksa:** şablonun tamamıyla oluştur. **Dosya varsa:** `servers` altına yalnızca `"archi-service-test"` anahtarını ekle/güncelle, `inputs` girdilerini aynı `id` yoksa ekle.
- `%USERPROFILE%\.service-test-mcp` dışında bir yere kurulduysa (`STM_INSTALL_HOME`), `command` alanına tam yolu yaz.

Dosyayı yazdıktan sonra geçerli JSON olduğunu kontrol et.

### Adım 5: Copilot custom agent ve yönergeler (isteğe bağlı)

Kullanıcıya "Servis testleri için hazır bir custom agent ister misin?" diye sor. Evet derse kullanıcının proje klasörüne:

- `templates/service-tester.agent.md` → `.github/agents/service-tester.agent.md`
- `templates/copilot-instructions.md` içeriği → `.github/copilot-instructions.md` (dosya varsa sonuna ekle)

### Adım 6: Kullanıcıya son adımları söyle

1. `Ctrl+Shift+P` → **MCP: List Servers** → **archi-service-test** → **Start Server**. Güven onayı gelirse **Trust** deyin.
2. Şifre/token tanımlandıysa VS Code her birini bir kez soracak ve güvenli depoda saklayacak.
3. Copilot Chat → **Agent** modu → araç (🔧) simgesinde `archi-service-test` altında 8 araç görünmeli (`list_tests`, `run_test`, `call_operation`, `generate_tests`, `save_definition`, `import_log`, `check_connection`, `end_sessions`). Ham SOAP/REST araçları için `mcp.json` içinde `"env": {"STM_TOOLSET": "uzman"}`.
4. Test için: **"archi-service-test ile tanımlı testleri listele"**, sonra **"Kur bilgisi listeleme testini <müşteri no> ile prep'te çalıştır"**. Demo için (demo sunucusu açıkken): **"Kur bilgisi listeleme testini 1000001 ile demo ortamında çalıştır"**.
5. Sorun olursa: **MCP: List Servers** → **archi-service-test** → **Show Output**.

Kurulum burada biter.

---

## Sorun giderme

| Belirti | Neden / Çözüm |
|---|---|
| Ajan yalnızca "zaman aşımı" / "hata" diyor | Ajandan sonucun `akis` satırlarını, hatanın `asama`, `zincir` ve `olasi_nedenler` alanlarını göstermesini isteyin; tam iz `iz_dosyasi` yolundaki JSON dosyasındadır (`%USERPROFILE%\.service-test-mcp\izler\`). Sonra **"check_connection ile prep bağlantısını kontrol et"** deyin. |
| `sunucu N sn içinde yanıt vermedi` (ReadTimeout) | Bağlantı kuruldu ama yanıt gelmedi: sunucu yavaş ya da istek takıldı. Sistem tanımında ortama `zaman_asimi_sn: 60` yazın; isteği `iz` içinde kontrol edin. |
| `bağlantı kurulamadı` (ConnectTimeout) / `DNS` | Bu bilgisayardan sunucuya yol yok: VPN, kurum ağı veya proxy. Tarayıcı proxy ile çıkıyorsa sistem tanımında ortama `proxy: http://...` yazın. `check_connection` hangi adımda koptuğunu gösterir. |
| `install.cmd` → EXIT 10 | Python 3.10–3.14 yok veya PATH'te değil. Python 3.12'yi kurun ("Add to PATH" ve "py launcher" seçili), VS Code'u yeniden açın. |
| EXIT 11 | `%USERPROFILE%` yazılamıyor ya da antivirüs engelliyor. `set STM_INSTALL_HOME=D:\Araclar\service-test-mcp` ile başka klasöre kurun. |
| EXIT 12 | `wheelhouse` eksik veya Python sürümü desteklenmiyor (3.15+). Zip'i yeniden açın ya da Python 3.12 kullanın. |
| EXIT 14 | Zip ile Python'un mimarisi farklı. Mesajda adı geçen diğer zip'i kullanın. |
| EXIT 15 / 16 / `No module named 'pywintypes'` | Yarım veya farklı mimaride eski kurulum. `%USERPROFILE%\.service-test-mcp\Scripts` ve `Lib` klasörlerini silip `install.cmd`'yi tekrar çalıştırın (`services.yaml` ve `logs` korunabilir). |
| `environment variable 'STM_...' is not set` | `services.yaml`'daki `*_env` adı `mcp.json` `env` bölümünde yok veya farklı yazılmış. Adları aynı yapın, sunucuyu **Restart** edin. |
| `Host '...' is not in the allowlist` | WSDL, yapılandırmadakinden farklı bir adres yayınlıyor (WCF'de makine adı / load balancer). O ortam için `endpoint` yazın veya host'u `allowed_hosts`'a ekleyin. |
| `401 Unauthorized` (SOAP veya REST) | Kimlik doğrulama tipi yanlış. WCF `Windows` güvenliği için `windows` (şifresiz) veya `ntlm`; IIS basic için `basic`. |
| `SSL` / `CERTIFICATE_VERIFY_FAILED` | Kurum CA sertifikası tanınmıyor. Ortamda `verify_tls: C:\certs\kurum-ca.pem` yazın (yalnızca test ortamında gerekirse `false`). |
| `Max retries` / `Failed to resolve` | Adres yanlış, VPN/proxy gerekli. Ortamda `proxy:` tanımlayın. |
| `The WSDL has no SOAP port` | Servis yalnızca `netTcpBinding` / `netNamedPipeBinding` sunuyor. Servis ekibinden test ortamı için `basicHttpBinding` endpoint'i isteyin. |
| `is marked as mutating` | Operasyon `mutating_operations` listesinde. Gerçekten gerekiyorsa yalnızca test ortamında `allow_mutating: true` yapın. |
| `... tanımlı değil` | Bu işlem ya da test henüz tanımlanmamış. İşlem için o ekranın logunu `import_log` ile verin; test için `generate_tests` ya da ajandan yazmasını isteyin. |
| `secret is not set` / `gizli.yaml` | Gizli değerler yüklenmemiş. Logu `import_log(save_secrets=true)` ile verin ya da `STM_SECRET_...` ortam değişkenlerini `mcp.json` içine ekleyin. |
| `is unknown (not classified ...)` | Katalogda `kind: unknown`. Ortam `allow_mutating: false` ise çağrılamaz; katalogda `read` / `mutating` / `auth` olarak işaretleyin. |
| `Test customer '...' is in use` | Bu test müşterisiyle başka bir pencere, ajan veya CI şu an login. Paralel login kapalıdır. `end_sessions` ile kendi oturumlarınızı kapatın, havuza müşteri ekleyin ya da başka bir test müşterisi kullanın. Kilit, sahibi kapanınca en geç session süresi (5 dk) sonunda düşer. |
| `login step ... failed` | Login adımlarından biri `Success: false` döndü. Test müşterisi bilgilerini, OTP'yi ve uygulama şifresini (`STM_SECRET_...`) kontrol edin. |
| Sunucu başlıyor ama araçlar görünmüyor | Chat modu **Agent** olmalı. Copilot Business/Enterprise'da "MCP servers in Copilot" politikası açık olmalı. |
| `spawn ... ENOENT` | `command` yolu yanlış; `%USERPROFILE%\.service-test-mcp\Scripts\service-test-mcp.exe` var mı kontrol edin. |

## Güncelleme ve kaldırma

- **Güncelleme:** Yeni zip'i açın ve `install.cmd`'yi tekrar çalıştırın; `mcp.json` ve `services.yaml` değişmez.
- **Kaldırma:** `%USERPROFILE%\.service-test-mcp` klasörünü silin ve `mcp.json` içinden `archi-service-test` girdisini kaldırın.
