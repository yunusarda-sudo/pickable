# Randevuo / Solveny güvenilirlik incelemesi

11 Ekim 2026. Hedef: `randevuo.com.tr`. Tarama ürünü: `solveny.dev` (Pickable).
Bu belge iki sağlayıcı çıktısını ve sınırlı bağımsız HTTP kontrollerini karşılaştırır; gerçek rezervasyon/ödeme yapılmadı.

## Canlı karşılaştırma

| Kaynak | Sonuç | Kanıtın zamanı ve anlamı |
|---|---|---|
| [Cloudflare Agent Readiness](https://isitagentready.com/randevuo.com.tr) | Arayüz 87/100, API level 5 / Agent-Native | API scannedAt 08:10:34 UTC / 11:10:34 Türkiye; 22 ham kontrol. API sayısal puan vermiyor. |
| Ora | 37/D | 04:38:54 UTC / 07:38:54 Türkiye; servedFromCache=true, resultAgeSeconds=12698. |
| Solveny, mevcut canlı eşleme | 42.8/100 | Yedi tanımlı kontrolde ağırlıklı kapsam %100. Eski Ora bulguları güncel Cloudflare bulgularını eziyor. |
| Yerel düzeltilmiş eşleme | 85.4/100, kapsam %84.4 | Aynı ham JSON ile hesaplandı. Beş PASS, bir FAIL, bir UNKNOWN. Canlıya yayımlandığı anlamına gelmez. |

[Canlı rapor JSON](https://solveny.dev/api/scans/2ecd68ff-a55e-4e87-a4df-de55e8a7a664/report),
[Ora ham kanıtı](https://solveny.dev/api/scans/2ecd68ff-a55e-4e87-a4df-de55e8a7a664/raw/ora),
[Cloudflare ham kanıtı](https://solveny.dev/api/scans/2ecd68ff-a55e-4e87-a4df-de55e8a7a664/raw/cloudflare).
Bağlantılar saklama süresine bağlıdır. Sonuçlar bu ölçümün anlık görüntüsüdür.

## Solveny'de giderilen sorunlar

- Kesin sonuçlar artık yalnız başarılı kaynaklardan alınır; bozuk/hatalı Cloudflare JSON yanıtı tamamlanmış tarama sayılmaz.
- Aynı ölçütü değerlendiren kaynaklarda daha yeni ölçüm kullanılır. Eşit veya karşılaştırılamayan tarihlerde Ora önceliği korunur; tarihsiz önbellekli bulgu tarihli başka bir bulguyu ezmez.
- Önbellekli Ora raporunda ölçüm tarihi yoksa geçerli yaş ve alım tarihinden türetilir; alım tarihi doğrudan güncel ölçüm olarak sunulmaz.
- Kaynak çelişkileri, önbellek yaşı ve seçilen kaynak arayüzde görünür.
- Cloudflare Markdown PASS, Vary: Accept doğrulaması gerektiren daha sıkı kontrolün yerine geçmez. Eski Ora FAIL ile daha yeni Cloudflare PASS varsa tam kontrol yeniden yapılana kadar UNKNOWN kalır.
- Skor yalnız yedi kanonik kontrolün ön değerlendirmesidir. Kaynaklara özgü diğer bulgular ham kanıtta korunur.

## Randevuo'da öncelikli bulgular

| Öncelik | Bulgu | Kanıt / yapılacak |
|---|---|---|
| P1 | Metadata ile çalışır uç noktalar arasında tutarsızlık riski | MCP kartının ilan ettiği `/mcp` adresine GET 404; OAuth metadata'sındaki `/oauth/authorize` adresine parametresiz GET 404; `/.well-known/jwks.json` anahtar listesi boş. GET tek başına MCP transportunu kesin olarak çürütmez. Geçerli initialize/tools-list ve OAuth authorize/token/JWKS akışları ayrıca test edilmeli. |
| P1 | OAuth Protected Resource uyumsuzluğu | `/.well-known/oauth-protected-resource` HTTP 200; resource değeri `https://randevuo.com.tr/api`. Cloudflare taranan origin ile eşleşmediğini bildiriyor. Amaçlanan API/MCP kaynağı ve metadata konumu standarda uygun hizalanmalı; yalnız puan için değeri değiştirmemeli. |
| P2 | DNS-AID keşfi eksik | Cloudflare DNS keşif kaydı bulamıyor. Kullanılacak keşif standardı ve desteklenen kayıtlar doğrulanarak eklenmeli. |
| P2 | A2A kartı yok | `/.well-known/agent-card.json` GET 404; sağlayıcı ham sonucu FAIL. A2A desteği ürün kapsamında ise uygula, değilse isteğe bağlı/n.a. olarak ayır. |
| P2 | Ödeme ve randevu yeteneği henüz ispatlanmış değil | OpenAPI ödeme metadata'sı ve ilan edilen operasyonlar bulundu; gerçek yetkili işlem, tutar/para birimi, iptal ve tekrar deneme doğrulanmadı. Sandbox kabul testi gerekli. |

Bağımsız ana sayfa isteğinde `Accept: text/markdown` için 200, Markdown içerik tipi, dolu içerik ve `Vary: Accept,Accept-Encoding` görüldü. Bu yeni gözlem eski Ora Markdown hatasının güncel olmayabileceğini destekliyor; iki sağlayıcının eski ham çıktısı değiştirilmedi, bağımsız gözlem ürün skoruna gizlice katılmadı.

## Ürün için kalan işler

1. Metadata keşfi, protokol doğrulaması ve gerçek görev başarısını ayrı göstermek; bunları tek başarı etiketiyle birleştirmemek.
2. Kaynaklara özgü DNS-AID, robots bot politikaları, API katalog/skills ve commerce bulgularını anlaşılır ek kartlarda sunmak. Yeni puan kapsamı için rubrik sürümünü ayrıca değiştirmek.
3. Her raporda “ölçüldü / kaynak bildirdi / çalışması doğrulanmadı” ayrımı ve gerçek gözlem zamanı.
4. Kullanıcıya içerik sitesi / API uygulaması profili sunmak; isteğe bağlı commerce veya A2A eksiklerini her site için zorunlu hata saymamak.
5. Cloudflare'ın halka açık tarama uç noktasının belgelenmiş API/SLA taahhüdü bulunmadan kesintisiz hizmet garantisi vermemek; mevcut kaynak hatasını açık göstermek.

[Cloudflare kontrol kapsamı](https://isitagentready.com/) keşif, içerik, bot politikası, protokol ve commerce standartlarına odaklanır. Yüksek keşif puanı, gerçek kullanıcı işlemlerinin uçtan uca çalıştığına dair kanıt değildir.

## Doğrulama

63 Python testi, 16 JavaScript testi ve JavaScript sözdizimi kontrolü geçti. Kod incelemesinde bulunan önbellek zaman hatası önce başarısız testle yeniden üretildi ve düzeltildi. Güvenlik incelemesinde uygulanabilir yeni bulgu yok. Üretime yayın bu belgeyle doğrulanmış değildir.
