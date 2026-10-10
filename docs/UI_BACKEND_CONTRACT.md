# Arayüz ↔ backend sözleşmesi

Bu sohbetin sınırı: `web/`, `app/reporting.py`, `tests/test_core.py`,
`tests/test_reporting.py`, UI smoke testleri ve bu doküman.
Backend adapter/depolama/orchestrator ve gerçek API sunucusu diğer sohbetin sınırıdır.
Ortak dosyaları değiştirmeden önce güncel hâlini okuyun.

## Endpoint'ler

- `GET /api/config` → `{ "csrf_token": "sunucu oturumu tokenı", "mode": "live" }`
- `POST /api/scans`, JSON `{ "domain": "randevuo.com.tr" }`, `X-CSRF-Token` header
  → HTTP 202 `{ "job_id": "..." }`. JSON `{ "error": "açıklama" }` hataları.
- `GET /api/scans` → `{ "scans": [{"id":"...","domain":"...","status":"completed","created_at":"ISO"}] }`
- `GET /api/scans/:id` → `{ "id":"...", "domain":"...", "status":"running|completed|interrupted|failed", "sources":[], "report": null|UnifiedReport }`
- `GET /api/scans/:id/report` → rapor JSON'u (download).
- `GET /api/scans/:id/raw/:source` → değiştirilmemiş sağlayıcı JSON'u (download).
- Kaynak id'si: `ora`. Kaynak durumları:
  `pending|running|completed|partial|unavailable|failed|timeout|interrupted`.
- Origin/Host doğrulama ve tarama tokenı backend sorumluluğudur. CORS açılmamalı.

## Rapor üretimi

`from app.reporting import report_from_job`

`report_from_job(job, source_runs)` fonksiyonu SQLite'dan alınan job ve kaynak
run listesine uygulanır. Kaynak run alanları: `source_id`, `status`, `raw_report`
(JSON string veya dict), `error`, `finished_at`. Ham raporlar yerinde değişmez.

Alternatif doğrudan arayüz:

```python
from app.reporting import build_report
report = build_report("randevuo.com.tr", {
  "ora": {"status": "completed", "raw": ora_json, "observed_at": "ISO"},
})
```

## Skor

- Sözlük 7 Ora kontrolü içerir. Tek kaynak Ora'dır.
- `scoring_status`: Ora'nın normalize edilmiş durumu. `combined_status`: PASS/FAIL ise aynısı, değilse UNKNOWN.
- Payda yalnız PASS+FAIL; uyarı, UNKNOWN ve N/A dışarıda. Ölçülmeyen katman null.
- Ağırlıklar 20/25/35/10/10. İlk sürüm güvenlik ve ödemeyi puanlamaz.
  Tanımlı kontrollerin tümü geçtiğinde ön skor 80/100, kapsam %80 olur;
  henüz ölçülmeyen alanlardan ücretsiz puan veya doğrulama üretilmez.
- `score.coverage`: ağırlıklı kesin ölçüm kapsamı, `score.rubric_version`: pickable-0.1.
- Ham kaynak durumları, skorlar, öneri sırası korunur. `source_specific` eşleşmeyen
  bütün bulguları saklar. Kaynak metinlerini arayüz yalnız textContent ile gösterir.

## Yerel UI doğrulaması

`python3 tests/ui_preview.py` yalnız fixture temelli test sunucusudur; canlı tarama
yapmaz. UI'da TEST MODU banner'ı görünür. Gerçek backend değildir.
