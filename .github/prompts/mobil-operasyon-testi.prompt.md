---
mode: agent
description: Bir işlemi seçilen test yaklaşımıyla tara ve sonucu raporla
tools: ['archi-service-test/*']
---
`${input:islem}` işlemini `${input:yaklasim}` yaklaşımıyla, `${input:musteri}` müşterisiyle `${input:ortam}`
ortamında test et:

1. `list_tests` ile işlem ve yaklaşım tanımlı mı bak; değilse söyle ve nasıl tanımlanacağını öner.
2. `generate_tests` ile taslak tabloyu üret; `notlar` varsa (eksik sınır bilgisi) bana sor.
3. `run_test(content=..., customer=..., environment=...)` ile koş.
4. Sonucu sade anlat: geçenler, kalanlar (beklenen ve gerçek), gözlemler (her biri için "doğru mu?" diye sor).
5. Onaylarsam testi `save_definition` ile kaydet; gözlem kararlarımı beklenen sonuca işle. Bitince `end_sessions`.
