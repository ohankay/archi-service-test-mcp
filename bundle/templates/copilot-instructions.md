## Servis testleri

- Servis cagrilarinda terminal, PowerShell, curl veya `Invoke-WebRequest` KULLANMA; `service-test` MCP araclarini kullan.
- Profilli servisler (ornegin mobil bankacilik WCF) operasyon adiyla cagrilir: `list_operations`, `describe_operation`,
  `call_operation(service, operation, params, persona)`. Login, OTP, session ve zarf alanlarini sunucu ekler; session
  anahtari, token veya sifre gonderme.
- Test musterisi = `persona` (`list_test_customers`). Ayni musteriyle paralel login yapilamaz; persona doluysa
  baska persona veya havuz kullan. Is bitince `end_sessions`.
- Kullanici bir log dosyasi verirse `import_traffic_log(path)` ile oku (onizleme), ozetle, onay alinca `apply=true`.
- Profilsiz SOAP/REST servisleri: `describe_operation`, `soap_call`, `rest_call`; token ve id'leri `extract` ile sakla.
- Tekrarlanacak kontrolleri `scenarios/` altina YAML senaryo olarak yaz, `validate_scenario` ile dogrula,
  `run_scenario` / `run_test_suite` ile kostur.
- Maskelenmis degerleri (`***`) tahmin etme.
