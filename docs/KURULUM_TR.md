# HACS özel depo kurulumu

Bu entegrasyon HACS genel listesinde değildir; özel depo olarak eklenir.
Gerçek HA kurulumu, beş kontrol ve sessiz dosya yükleme doğrulanmıştır.
Bu yeni bileşenin fiziksel ses testi henüz yapılmamıştır.

## Gerekenler

- Home Assistant OS/Supervisor, Core 2026.10 veya üzeri.
- Çalışan MQTT entegrasyonu ve broker.
- Hikvision Doorbell uygulaması, kararlı sürüm 3.0.34 veya üzeri:
  https://github.com/pergolafabio/Hikvision-Addons
- İç ekranın yerel IP adresi, SDK portu (genellikle 8000), cihaz admin
  kullanıcı adı ve parolası. Hik-Connect hesap parolası farklıdır.

Windows, SADP veya iVMS çalışma sırasında gerekmez. Yalnız MQTT yeterli değildir;
ses aktarımını Home Assistant üzerindeki Hikvision Doorbell uygulaması yapar.

## Kurulum ve kullanım

1. Önce Home Assistant yedeği al. İlk değerlendirmeyi ayrı test ortamında yap.
2. HACS → üç nokta → Özel depolar: aşağıdaki adresi **Entegrasyon** türüyle ekle:
   `https://github.com/berkkayhan34-sys/hikvision-intercom-chime`.
   Hikvision Intercom Chime deposunu bulup indir. Alternatif olarak ZIP içindeki
   `custom_components/hikvision_intercom_chime` klasörünü HA'ya kopyalayabilirsin.
3. Home Assistant'ı yeniden başlat. Ayarlar → Cihazlar ve hizmetler →
   Entegrasyon ekle → Hikvision Intercom Chime.
4. Bağlantı bilgilerini yalnız kurulum ekranına gir. Eksik MQTT/köprü için
   açıklama gösterilir. Köprü yeniden başlatılacaksa ayrıca onay istenir;
   aynı uygulamadaki diğer paneller kısa süre bağlantı kaybedebilir.
5. Entegrasyonun Yapılandır ekranından WAV yükle, gain ve tekrar sayısını seç.
   Dosya: **8000 Hz / mono / PCM16 WAV**, en fazla 1 MiB / 30 saniye.
   Tekrarlarla toplam en fazla 60 saniye; tekrar sayısı 1–10.
6. Gain ve tekrar cihaz sayfasındaki sayı kontrollerinden de değiştirilebilir.
   Gain dijitaldir; artışı sesi bozabilir, fiziksel hoparlör sınırını yükseltmez.
7. Yalnız **Zili çal** düğmesi ses isteği gönderir. Kurulum, kaydetme ve
   yeniden yükleme sessizdir. Varsayılan ses önceden kabul edilen ding-dong,
   dört tekrar ve 0 dB ek gain'dir.

Parola entegrasyon kaydına değil, mevcut köprünün Supervisor ayarlarına yazılır.
Yedeklerini koru. Entegrasyonu kaldırmak köprüyü/MQTT'yi veya onların parolalarını
silmez. “Ses istendi” durumu, sesin fiziksel olarak duyulduğunu kanıtlamaz.
