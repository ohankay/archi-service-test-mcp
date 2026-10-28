---
mode: agent
description: Kalan bir testin nedenini incele
tools: ['archi-service-test/*']
---
`${input:test}` testi `${input:musteri}` müşterisiyle kaldı. Nedenini bul:

1. `run_test` ile tekrar koş; kalan kontrolü, beklenen ve gerçek değeri göster.
2. Gerekirse ilgili işlemi `call_operation` ile tek başına çağırıp yanıtı incele.
3. Sorun serviste mi (beklenen davranış bu değil), testte mi (kontrol yanlış / veri değişmiş) yoksa ortamda mı
   (bağlantı, oturum) ayır. Testte ise düzeltme öner ama kontrolü sırf geçsin diye gevşetme.
