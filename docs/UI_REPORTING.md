# Arayüz ve raporlama

Bu çalışma basit Türkçe arayüzü (`web/`), kaynak raporlarını birleştiren
`app/reporting.py` modülünü ve bunların testlerini kapsar. Adapter, SQLite,
orchestrator ve canlı API sunucusu aynı projede çalışan diğer sohbetin kapsamıdır.

## Yerel önizleme

Proje kökünde:

```sh
python3 server.py --demo --port 8765
```

Ardından <http://127.0.0.1:8765> adresini açın. Açılış ekranında boş domain
alanı bulunur; analiz başlatılınca yükleme ekranı, ardından rapor gösterilir.
Raporda özet, sonraki adımlar, katmanlara ayrılmış kontroller, kontrol tablosu,
filtreleme, ham kanıt ve JSON indirme denenebilir. Oturum içi rapor geçmişi üst menüdeki düğmeden
açılan pencerededir; yeni analiz başlatmak açılış ekranına geri döndürür.

**Bu sunucu yalnız sentetik test verisi kullanır; girilen domaini taramaz.**
Ekranda TEST MODU etiketi gösterilir. Geçmiş bellektedir, yeniden başlatılınca
silinir; oturum 30 raporla sınırlıdır. Bu önizlemenin çalışması gerçek Ora
entegrasyonunun çalıştığını kanıtlamaz.

## Raporun görsel düzeni

Rapor önce domaini ve analiz zamanını, ardından büyük ön skor, ölçüm kapsamı,
genel değerlendirme ve kontrol sayılarını gösterir. Katman puanlarının altında
iki bölüm bulunur: eksik bildirilen kontroller için sonraki adımlar ve
katmanlara ayrılmış, ayrıntıları açılabilen kontroller. Kaynak skoru, yöntem,
kontrol tablosu, ham kanıtlar ve orijinal rapor indirmeleri
**Kaynaklar & yöntem** bölümündedir. Böylece teknik ayrıntılar korunurken
raporun ilk görünümünde sonuç ve yapılabilecekler öne çıkar.

Görsel hiyerarşi için incelenen referanslar:

- [Ora'nın ora.ai raporu](https://ora.ai/score/ora.ai): büyük skor ve hedefleri
  öne çıkaran rapor düzeni.
- [Cloudflare domaininin AgentReady raporu](https://isitagentready.com/cloudflare.com):
  skor, kategoriler ve kontrol ayrıntılarındaki hedef / sorun / uygulama düzeni.

Bu referanslar sunum düzenine ilham verir. Cloudflare entegrasyonu eklenmez;
Pickable'ın skor hesabı ve kontrol eşleştirmeleri değişmez. Cloudflare domainine
ait raporun incelenmesi, Cloudflare'ın kendi ürünüyle bağlantı kurulduğu anlamına
gelmez.

Sonraki adımlar, eksik bildirilen kontrollerden üretilen genel Pickable
önerileridir; dayandıkları kaynak etiketiyle gösterilir. Sağlayıcının özgün
önerisi veya öncelik sıralaması olarak sunulmaz; etki, uygulama süresi ya da
skor artışı tahmini içermez. Sağlayıcı önerilerinin orijinal sırası teknik
ayrıntılarda korunur.

## Skorun anlamı

Sağlayıcı skorları ayrı gösterilir; ürün skoru bunların ortalaması değildir.
Yedi kanonik kontrol üzerinden her katman için `PASS / (PASS + FAIL) × ağırlık`
hesaplanır. Ağırlıklar keşfedilebilirlik 20, anlaşılabilirlik 25,
kullanılabilirlik 35, güvenlik ve uyum 10, ödeme 10'dur.

UNKNOWN, uyarı ve uygulanamaz sonuçlar paydadan çıkarılır. Ölçülmeyen katman
`null` kalır; kesin ölçüm kapsamı ayrıca gösterilir. İlk sürüm güvenlik ve
ödemeyi puanlamaz: tanımlı kontrollerin tümü geçse de sonuç 80/100 ve %80 kapsamdır.
Bu nedenle arayüz sonucu **ön skor** olarak adlandırır.

Tek kaynak Ora'dır; her kontrolün puan girdisi Ora'nın bildirdiği PASS/FAIL'dir.
Bağımsız doğrulama iddia edilmez.

## Kaynakların korunması ve entegrasyon

Native kontrol kimlikleri, durumları, kanıtları, kaynak skorları ve zamanları
raporda korunur. Eşleşmeyen bulgular ayrı gösterilir; Ora önerilerinin sırası
değiştirilmez. Sağlayıcı metinleri HTML olarak yorumlanmadan gösterilir.

Canlı backend bağlantısı için [API sözleşmesini](UI_BACKEND_CONTRACT.md)
izleyin. Saklanan job ve kaynak run kayıtları `report_from_job(job, source_runs)`
ile arayüzün beklediği rapora çevrilebilir. Ham indirme endpoint'i sağlayıcının
saklanan orijinal raporunu döndürmelidir. Canlı sunucu `/api/config` yanıtında
`mode: "live"` kullanmalı; preview için bu değer `"demo"`dur.

## Doğrulama komutları

Bu sohbetin kapsamındaki testleri bağımsız çalıştırmak için:

```sh
python3 -m unittest discover -s tests -p test_core.py -v
python3 -m unittest discover -s tests -p test_reporting.py -v
python3 -m unittest discover -s tests -p test_ui_api.py -v
node --check web/app.js
node --test tests/test_web_state.cjs
```

Bunlar skor/kapsam, kaynak durumları ve kanıt korunması, domain doğrulama,
fixture API akışı, JSON indirme ve preview erişim kontrollerini sınar.
13 Node ve 28 Python testi; ekran geçişlerini, eski raporun temizlenmesini,
geçmiş seçimindeki asenkron hata yarışlarını ve bozuk sağlayıcı alanlarının
güvenli gösterimini de denetler.
Gerçek sağlayıcı taraması ve canlı backend'in uçtan uca doğrulaması ayrı kabul
adımıdır; bu testlerden geçtiği sonucu çıkarılamaz.
