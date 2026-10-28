---
mode: agent
description: Uygulama (veya HAR) logundan yeni işlemleri tanımla
tools: ['archi-service-test/*']
---
`${input:log_dosyasi}` log dosyasını değerlendir:

1. `import_log` ile ÖNİZLEME yap (apply vermeden). Logu kendin okumaya çalışma; dosyayı sunucu okur.
2. Bana özetle: hangi ortam bulundu, hangi işlemler yeni, hangileri zaten tanımlı (tanımda olmayan yeni alan var mı),
   hangi çağrılar gürültü veya sistem dışı sayıldı.
3. Her yeni işlem için Türkçe bir ad ve hangi girdilerin önemli olduğunu sor; tarih ve sayı girdileri için sınır
   bilgisi (en az / en fazla, tarih aralığı) biliniyor mu diye sor.
4. Onay verirsem `apply=true` ile taslakları kaydet, sonra kararlarımı `save_definition` ile işle.
5. İlk gerçek çağrıyı `call_operation` ile yap ve yanıta göre önerdiğin kontrolleri göster.
