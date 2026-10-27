---
mode: agent
description: Bir servis operasyonu icin YAML test senaryosu uret ve kostur
tools: ['service-test/*', 'edit']
---
`${input:servis}` servisinin `${input:operasyon}` operasyonu icin test senaryosu hazirla:

1. `describe_operation` ile girdi/cikti semasini incele.
2. Bir pozitif ve en az iki negatif durum icin adimlari tasarla (eksik/gecersiz girdi, yetkisiz erisim).
3. `scenarios/` altina YAML olarak yaz; her adimda `fault`/`status` ve is kurali assertion'lari olsun.
4. `validate_scenario` ile dogrula, `run_scenario` ile kostur ve sonucu adim adim raporla.
