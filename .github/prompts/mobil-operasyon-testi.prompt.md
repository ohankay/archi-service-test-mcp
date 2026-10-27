---
mode: agent
description: Mobil bankacilik operasyonu icin pozitif ve negatif senaryo yaz ve kostur
tools: ['service-test/*', 'edit']
---
`${input:servis}` servisinin `${input:operasyon}` operasyonunu `${input:ortam}` ortaminda test et:

1. `describe_operation` ile parametreleri, turunu ve yanit semasini incele; `list_test_customers` ile uygun test musterisini sec.
2. `call_operation` ile bir kez dene; yaniti ozetle.
3. `scenarios/` altina YAML senaryo yaz: bir pozitif durum (`success: true` ve is kurali assertion'lari) ve en az iki
   negatif durum (gecersiz / eksik parametre, sinir degerleri; beklenen `success: false` ve hata kodu).
4. `validate_scenario`, sonra `run_scenario` ile kostur; adim adim sonucu ve basarisiz assertion'lari raporla.
5. Bitince `end_sessions`.
