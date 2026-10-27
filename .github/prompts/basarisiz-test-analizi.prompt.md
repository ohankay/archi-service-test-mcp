---
mode: agent
description: Basarisiz bir senaryoyu analiz et
tools: ['service-test/*', 'search']
---
`${input:senaryo}` senaryosunu `run_scenario` (verbosity: full) ile kostur. Basarisiz her assertion icin beklenen ve
gercek degeri karsilastir; hatanin servis davranisindan mi, test verisinden mi, senaryodaki yanlis bir beklentiden mi
kaynaklandigini gerekcesiyle belirt. Assertion'i gevsetmeyi onerme; gerekiyorsa servis ekibine iletilecek kisa bir
hata ozeti yaz.
