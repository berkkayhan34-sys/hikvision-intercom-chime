# Kurulum rehberi (Türkçe)

Tek entegrasyon + tek küçük uygulama. Kapı açma, arama, ses seviyeleri ve kamera
Hik-Connect hesabınızla çalışır; iç ünitede özel zil sesi için ek olarak
**Hikvision SDK Bridge** uygulaması gerekir.

## Gereksinimler

- Home Assistant 2026.10.0 veya üstü (zil sesi için Home Assistant OS ya da Supervised).
- İstasyonu taşıyan Hik-Connect hesabı.
- Zil sesi için: iç ünitenin yerel IP adresi (özel ağ), SDK portu (genelde 8000) ve **cihaz
  yönetici** parolası (Hik-Connect parolası değildir).

## Adımlar

1. HACS -> Özel depolar -> `https://github.com/berkkayhan34-sys/hikvision-intercom-chime`
   (Entegrasyon) -> **Hikvision Intercom** indir -> Home Assistant'ı yeniden başlat.
2. Ayarlar -> Uygulamalar -> Uygulama mağazası -> sağ üst menü -> Depolar -> aynı adresi
   ekleyin -> **Hikvision SDK Bridge** kurun (ilk derleme cihazınızda yapılır, yaklaşık 26 MB
   indirir). Durdurulmuş kalabilir; entegrasyon başlatır. Uygulama olmadan entegrasyon
   çalışmaz; kurulum, uygulama kurulana kadar bir kurulum istemi gösterir.
3. Ayarlar -> Cihazlar ve hizmetler -> Entegrasyon ekle -> **Hikvision Intercom**. Tek akış
   sırasıyla uygulamayı (yoksa) ister, Hik-Connect girişini alır, ardından iç ünitenin adresi,
   SDK portu ve yönetici bilgilerini sorar ve uygulamanın yeniden başlatılmasını onaylatır.
   Parola yalnızca uygulamanın kendi ayarlarında saklanır, entegrasyon kaydında asla.

## Kullanım

- **Zil sesini çal / Durdur**: yalnızca bastığınızda çalar. Kurulumda, yeniden yüklemede,
  ayar değişikliğinde asla çalmaz. İç ünite çalıyor veya görüşmedeyse engellenir.
- **Dijital kazanç** (-30..+30 dB) ve **Tekrar sayısı** (1..10, toplam en fazla 60 sn).
- Kendi sesiniz: istasyon kaydında **Yeniden yapılandır** -> WAV yükleyin (8000 Hz, mono,
  16 bit PCM, en çok 1 MiB / 30 sn).

## Eski kurulumdan geçiş

Home Assistant yedeği alın -> eski üç entegrasyonu ve *Hikvision Doorbell* uygulamasını
kaldırın -> yeniden başlatın -> uygulamayı kurun -> entegrasyonu ekleyin (tek akış).
Zil varlıkları Doorbell cihazına katılır; önceki sürümde kayıtlı varlıkların kimlikleri korunur.
Eski uygulamadaki ISAPI kutusu, yeniden başlatma, sessize alma ve MQTT çağrı sensörleri
taşınmaz.
