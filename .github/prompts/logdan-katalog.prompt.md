---
mode: agent
description: Mobil uygulama (veya HAR) logundan operasyon katalogunu guncelle ve taslak senaryo uret
tools: ['service-test/*', 'edit']
---
`${input:log_dosyasi}` log dosyasini katalog icin degerlendir:

1. `import_traffic_log` ile ONIZLEME yap (apply vermeden). Logu kendin okumaya calisma; dosyayi sunucu okur.
2. Bana ozetle: hangi servis ve ortam bulundu, yeni / degisen / degismeyen operasyonlar, degisikliklerin ayrintisi,
   turu `unknown` kalanlar, kimlik bilgisi alanlari ve zarfa tasinmasi onerilen alanlar.
3. `unknown` operasyonlar icin read / mutating / auth karari iste.
4. Onay verirsem `apply=true` ile katalogu yaz, sonra kararlarimi katalog dosyasina isle.
5. Istersem `scenario_path` ile `scenarios/_taslak-<ad>.yaml` taslak senaryosunu yaz; maskeli degerleri (`***`)
   `{{env.x}}` / persona alanlariyla degistir, `validate_scenario` ve `run_scenario` ile dene.
