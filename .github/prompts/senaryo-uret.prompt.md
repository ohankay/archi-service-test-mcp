---
mode: agent
description: Tarif edilen bir akış için adımlı test yaz ve koş
tools: ['archi-service-test/*']
---
Şu akış için bir test hazırla: `${input:akis}`

1. `list_tests` ile kullanılacak işlemleri bul; eksik işlem varsa söyle.
2. Adımlı bir test yaz (`adimlar`): bir adımın çıktısı sonrakinin girdisiyse `al` ile aktar; her adıma en az bir iş
   kuralı kontrolü koy; değişen veriler için kural yaz. Kontrol olarak yazılamayan beklentileri `ai_yorum` yap.
3. `run_test(content=..., customer=${input:musteri})` ile koş, sonucu adım adım anlat.
4. Onaylarsam `save_definition` ile kaydet.
