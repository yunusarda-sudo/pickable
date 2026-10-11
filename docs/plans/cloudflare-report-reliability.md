# Cloudflare rapor güvenilirliği — 11 Ekim 2026

## Karar ve kapsam

Cloudflare tarayıcı bağlantısı canlıda mevcut. Sorun, daha eski Ora sonuçlarının her zaman öncelikli olması ve farklı ölçütlerin aynı kabul edilmesi. Ham sağlayıcı raporları değişmeden korunacak; sağlayıcı puanları birleştirilmeyecek.

## Uygulama sırası

1. Hatalı Cloudflare yanıtı ve başarısız kaynakların puanlanması için önce başarısız testler.
2. Karşılaştırılabilir kesin bulgularda güncel gözlemi seç; eşit/belirsiz zamanda Ora önceliğini koru. Önbellekli tarihsiz bulgunun alım zamanını ölçüm zamanı sayma.
3. Cloudflare Markdown kontrolünü Ora'nın Vary şartıyla eşitleme; eski önbellek hatası için yeniden ölçüm gerektiğini göster.
4. Arayüzde önbelleği, zamanları, kaynak çelişkilerini ve kapsam farkını göster.
5. Gerçek Randevuo raporlarıyla yeniden hesapla; birim testleri, tarayıcı kontrolü, kod ve güvenlik incelemesi.

## Kabul ölçütleri

- Başarısız veya eksik sağlayıcı yanıtı puan kazandırmaz.
- Eski önbellek güncel karşılaştırılabilir bulguyu ezmez.
- Yedi kontrolün kapsamı tüm site yetenekleri olarak sunulmaz.
- Metadata başarısı çalışan kimlik doğrulama, rezervasyon veya ödeme garantisi sayılmaz.
- Canlı sonuç ile yerel düzeltme sonucu ayrı etiketlenir.
