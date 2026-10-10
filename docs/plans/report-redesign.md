# Rapor ekranı yeniden tasarımı

## Amaç ve sınır

Ana sayfa korunacak. Rapor, teknik karşılaştırma tablosu yerine önce sonucu ve yapılabilir işleri anlatacak. Ora'nın skor + katman + düzeltme akışı ve Cloudflare'ın kategori bazlı kontrol/guidance yaklaşımı referans alınacak; onların puanları veya doğrulama iddiaları kopyalanmayacak. Backend ve skor formülü değişmeyecek.

## Ürün hiyerarşisi

1. **Rapor başlığı:** domain, tarama zamanı, test verisi etiketi; JSON indirme ikincil eylem.
2. **Özet:** büyük Pickable ön skoru; hemen yanında ölçüm kapsamı. “Eksikleri incele” eylemi. PASS/FAIL/uyarı-belirsiz sayıları mevcut `scoring_status` değerlerinden hesaplanır. Başarı yüzdesi veya sahte önem derecesi üretilmez.
3. **Kategori gezintisi:** Tümü + beş katman. Her kategoride mevcut ölçülen kontrol / tanımlı kontrol ve puan; güvenlik/ödeme “Ölçülmedi”. Seçim, görünür kontrol listesini filtreler; puanı değiştirmez.
4. **Kontrol kartları:** teknik sütun tablosu yerine başlık, anlaşılır durum, ölçütün kısa açıklaması, kaynak etiketleri ve “Kanıtı incele”. Eksik kontroller önce; kaynakların farklı sonuçları ayrı amber etiketle gösterilir. `combined_status` ile `scoring_status` karıştırılmaz. Kısmi eşleşmenin tek kaynağa dayalı puan girdisi kartta okunabilir.
5. **Önerilen adımlar:** yalnız kaynağın gerçekten sağladığı öneriler okunabilir metin kartları olarak. Ora öneri sırası korunur; keyfî önem, puan artışı veya tahmini iş süresi eklenmez. Öneri yoksa ölçüt bilgisi ve kanıt bağlantısı sunulur; uydurma çözüm üretilmez.
6. **Kaynaklar ve yöntem:** Ora ve AgentReady kendi skor/seviyesi, durum, zaman ve ham JSON bağlantısı. Yöntem açıklaması ve kaynağa özel eşleşmeyen bulgular açılır bölümde. Kaynaklar ilk ekranı kaplamaz.

## Kanıt ve etkileşim

- Kaynağın yerel sonucu, karşılaştırma etiketi ve puana giren sonuç ayrı tutulur.
- Kanıt penceresinde önce okunabilir açıklama ve kaynak kimliği; ham JSON ayrıntı olarak korunur.
- API hatası, tek kaynak, hiç ölçüm yok, tümü PASS ve ölçülmeyen kategori ayrı boş/durum metinleriyle karşılanır.
- Kaynak metinleri yalnız `textContent`; HTML/URL çalıştırılmaz.
- Kategori düğmelerinde seçili durum; klavye odağı ve mobilde yatay kategori kaydırma.

## Kabul ve doğrulama

- Ana sayfanın tasarımı/akışı değişmez.
- İlk rapor ekranında domain, ön skor, kapsam ve sonraki eylem okunur; ham JSON görünmez.
- Demo fixture'ın 20/100 ve %34.2 kapsamı aynen kalır; kaynağın 61/D ve 1/5 sonuçları ayrı kalır.
- Her kanonik kontrol ve eşleşmeyen kaynak bulgusu erişilebilir; filtreli sayılar gerçek verilere dayanır.
- Mobil/masaüstü, kategori + durum filtresi, kanıt açma/kapama, JSON indirme ve yeni analiz geri dönüşü tarayıcıda doğrulanır.
- Eksik kaynak, null puan ve kötü niyetli kaynak metninin güvenli gösterimi regresyon testinde korunur.
