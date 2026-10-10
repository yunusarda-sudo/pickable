# Çok kaynaklı raporlama — ilk sürüm

10 Ekim 2026. Kullanıcı kapsamı: basit Türkçe arayüz ve çalışan backend;
Ora + Python AgentReady, kaynak raporlarını saklama, karşılaştırma ve ayrı 100'lük ürün skoru.

## Güncel iş bölümü

Kullanıcı aynı klasörde iki sohbetin çalıştığını doğruladı. Bu sohbet arayüz ve
raporlamayı tamamlar; adapter, depolama, orchestrator ve canlı API sunucusu diğer
sohbete aittir. Aşağıdaki liste ürünün genel planıdır, tamamlanma iddiası değildir.
Yerel UI önizlemesi yalnız sentetik fixture kullanır. Güncel kullanım ve sınırlar
[arayüz ve raporlama dokümanındadır](../UI_REPORTING.md).

## Uygulama sırası

1. Güvenlik ve skor testlerini önce yazıp kırmızı sonucu doğrula.
2. Public hedef doğrulama ve DNS'e sabitlenmiş HTTP taşıma katmanı.
3. Ora tarama/polling adapter'ı ve commit'e sabitlenmiş MIT scanner runner'ı.
4. SQLite ham rapor/iş saklama; iki kaynağı paralel çalıştırma, ayrı hata durumları.
5. Sürümlenmiş kontrol sözlüğü, karşılaştırma ve skor/kapsam hesabı.
6. Domain formu, kaynak ilerlemesi, kanıt tablosu, ham JSON indirme, geçmiş.
7. Otomatik testler, gerçek randevuo.com.tr taraması ve tarayıcı kontrolü.
8. Kod/güvenlik incelemesi ve kullanım dokümanı.

## Sınırlar ve kararlar

- Python standart kütüphanesi + SQLite + HTML/CSS/JS; framework/dependency kurulumu yok.
- Yalnız public domain, varsayılan portlar; her keşfedilmiş URL ve redirect için public egress.
- Vendor ağ erişimi güvenli fetch wrapper'ından geçer; subprocess süre/kaynak sınırı vardır.
- Sağlayıcı skorları korunur; ürün skoru bunların ortalaması değildir.
- PASS/FAIL dışı veriler paydadan çıkar. Tamamen ölçülmeyen katman null;
  puan verilmez. Eksik kapsam yüzde olarak raporlanır, puan ön skor olarak etiketlenir.
- Kısmi/ilişkili eşleşmeler doğrulanmış uzlaşma sayılmaz. Koşul eksikliği açıklanır.
- Ham rapor, native durumlar, sağlayıcı sürümü ve tarama zamanı korunur.
- Onarım, overlay, gerçek görev testi, Cloudflare ve yayınlama kapsam dışında.

## Kabul koşulları

- Tek kaynak hatası diğer kaynağın verisini kaybettirmez.
- UNKNOWN/N/A hiçbir zaman FAIL'e veya ücretsiz puana dönüşmez.
- Kullanıcı her bulgunun kaynağını ve ham kanıtını açabilir.
- Private IP, mixed DNS ve private redirect istekleri ağdan önce engellenir.
- Raporlar sunucu yeniden başladığında korunur; yarım işler kesildi olarak işaretlenir.
- Testler ve yerel runtime akışı geçer; gerçek sağlayıcı sorunları açıkça gösterilir.
