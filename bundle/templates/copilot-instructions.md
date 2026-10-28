## Servis testleri

- Servis çağrılarında terminal, PowerShell, curl veya `Invoke-WebRequest` KULLANMA; `service-test` MCP araçlarını kullan.
- Önce `list_tests`: tanımlı sistemler, işlemler, testler ve test yaklaşımları. Yalnızca buradaki adları kullan;
  tanımlı olmayan bir şey istenirse bunu söyle ve `import_log` (işlem için) ya da `generate_tests` (test için) öner.
- Test çalıştırma: `run_test(test, customer, environment)`. Login, OTP ve oturumu sunucu yapar.
- "X'i <yaklaşım> ile tara": `generate_tests(yaklaşım, işlem)` → taslağı göster → `run_test(content=...)` →
  kullanıcı onaylarsa `save_definition`.
- `gözlem` sonuçlarına karar verme: davranışı kullanıcıya göster, doğru mu diye sor. `ai_yorum` soruların yanıtı
  senin görüşündür; geçti/kaldı kararı değildir.
- Kontrol satırları: `başarılı`, `X sayısı >= 1`, `X boş değil`, `X eşleşir ^TR`, `her LISTE için A <= B`,
  `X arasında a ve b`. Değişen veri (bakiye, kur, tarih) için sabit değer değil kural yaz.
- Maskelenmiş değerleri (`***`) tahmin etme. İş bitince `end_sessions`.
