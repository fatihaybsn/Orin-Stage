# Orin Stage JP6.0 Ağır Doğrulama Özeti

Bu not, `jetson-orin@jp6.0` hedefi için bu oturumda yapılan ağır doğrulamanın kısa ama bütünlüklü özetidir. Amaç; SDK Manager keşfinden başlayıp indirme, doğrulama, base oluşturma, workspace açma, ARM64 QEMU çalıştırma, x86_64 üstünde cross-build ve aynı workspace içinde ARM64 binary çalıştırmaya kadar zincirin gerçekten çalıştığını görmekti.

## Başlangıç Durumu

İlk hedef sadece `jetson-orin@jp6.0` idi. JP6.1, JP6.2.1 ve JP6.2.2 kapsam dışı bırakıldı. Mevcut JP6.2.3 verilerine dokunulmaması istendi; sonradan disk alanı baskısı oluşunca yalnızca kullanıcı onayıyla eski JP6.2.3 workspace kaldırıldı, acquisition ve base verileri korunmuş oldu.

Aktif CLI olarak proje içindeki `.venv/bin/ostg` kullanıldı. `ostg doctor` çıktısı SDK Manager, Podman, QEMU/binfmt ve toolchain tarafında ortamın temel olarak hazır olduğunu gösterdi.

## Yapılan Ana İşler

Önce SDK Manager üzerinden JP6.0 keşfi yapıldı. Bu hedefin SDKM tarafında arşivlenmiş sürüm olduğu görüldü; bu yüzden indirme komutunun da `--archived-versions` ile çalışması gerektiği anlaşıldı ve kod buna göre düzeltildi.

Sonra JP6.0 artifact indirme ve doğrulama zinciri çalıştırıldı. NVIDIA'nın resmi dosya adları ve checksum kayıtları ile Orin Stage içindeki katalog dosya adları arasında büyük/küçük harf farkı olduğu görüldü. Burada gevşek veya tahmine dayalı eşleştirme yapılmadı; resmi checksum manifestindeki dosya adını esas alan net bir doğrulama davranışı eklendi.

Base oluşturma aşamasında birkaç önemli JP6.0 problemi yakalandı:

- JP6.0 için `nvidia-jetpack` meta-package sürümü katalogda kesin değildi. NVIDIA apt index içinde SDKM revizyonuna denk gelen sürümün `6.0+b106` olduğu doğrulandı ve katalog buna sabitlendi.
- APT simulation parser bazı NVIDIA paket satırlarını okuyamıyordu. Özellikle OpenCV ve L4T paketlerinde gelen ek köşeli parantez bölümleri desteklenecek şekilde parser düzeltildi.
- JP6.0 kurulumu Ubuntu'nun bazı OpenCV dev paketlerini kaldırıp NVIDIA/OpenCV paketleriyle değiştirmek istiyordu. Orin Stage normalde paket kaldırmayı yasakladığı için işlem durdu. Bunun genel bir silme izni olmaması gerektiğine karar verildi; sadece JP6.0'a özel, tam paket listesiyle sınırlı, açık bir removal allowlist eklendi.
- `nvidia-l4t-core` preinst script'i offline ARM64 chroot içinde `/proc/device-tree/compatible` beklediği için hata veriyordu. NVIDIA paketinin kendi desteklediği marker mekanizması kullanılarak bu firmware/device-tree kontrolü sadece base kurulumu sırasında devre dışı bırakıldı ve işlem bitince marker temizlendi.
- Hata mesajları ilk başta tek satıra kırpıldığı için gerçek sebep gizleniyordu. CLI ve privileged base tarafında çok satırlı hata çıktıları korunacak şekilde düzeltme yapıldı.

Bu düzeltmelerden sonra JP6.0 base başarıyla oluşturuldu ve doğrulandı.

## Doğrulanan Sonuçlar

JP6.0 acquisition cache-hit durumuna geldi; kritik BSP ve sample rootfs artifact'leri resmi SHA-1 değerleriyle ve kaydedilen SHA-256 değerleriyle doğrulandı.

Base başarıyla `constructed+validated` durumuna geçti:

- Target: `jetson-orin@jp6.0`
- Status: `validation-pending`, açıkça izin verilerek kullanıldı
- Meta-package: `nvidia-jetpack=6.0+b106`
- Target lock digest: `c8122cece3d943bd26e2efcfcb6e367d8151b69d0e0e35bbc61a0ea8c8f8af34`
- Base digest: `37f666f3eb3230c3789298452471e05db921d6d48401e6b0aa5bea6fd5f87025`

Ardından `jp60-validation` workspace oluşturuldu. Workspace generation `0` olarak başladı ve base digest/target lock digest eşleşmesi doğrulandı. Materialization parity kontrolü de geçti.

ARM64 QEMU çalıştırma tarafında aynı workspace içinde şu kontroller yapıldı:

- `uname -m` sonucu `aarch64`
- `dpkg --print-architecture` sonucu `arm64`
- deterministik SHA-256 hesaplama sonucu doğru
- `/bin/bash` çalışıyor
- `nvidia-jetpack` paketi `6.0+b106 arm64` olarak görülüyor
- `exit 42` doğru şekilde host tarafa `42` olarak taşınıyor ve başarısız run generation artırmıyor

Cross-build tarafında x86_64 host üzerinde, aynı workspace'e bağlı build ortamında küçük bir C programı derlendi. `/target` read-only doğrulandı, toolchain `/opt/toolchain` üstünden kullanıldı, çıktı ELF dosyasının `AArch64` olduğu ve interpreter olarak `/lib/ld-linux-aarch64.so.1` istediği doğrulandı. Üretilen ARM64 binary aynı JP6.0 workspace içine taşındı ve QEMU ile çalıştırıldığında `ORIN_STAGE_JP60_BUILD_OK` çıktısı verdi.

Son regresyon kontrolünde tam test takımı çalıştı ve sonuç `596 passed` oldu. `git diff --check` de temizdi.

## Sorular ve Cevaplar

Sorduğun ilk ana soru şuydu: "Bu hatalar benim bilgisayarıma özel mi, yoksa başkası JP6.0 kurunca da aynı şeyleri yaşar mı?"

Cevap: Yakalanan hataların büyük bölümü kişisel bilgisayara özel değildi. JP6.0'ın NVIDIA tarafındaki gerçek paket yapısından, SDK Manager arşiv davranışından, apt transaction içeriğinden ve offline chroot içinde L4T preinst script'lerinin çalışma şeklinden geliyordu. Yani bunlar başka bir kullanıcıda da görülme ihtimali yüksek olan genel JP6.0 sorunlarıydı. Kodda yaptığımız düzeltmeler release'e girerse, aynı JP6.0 hedefini kuran kullanıcı bu spesifik hataları normalde tekrar yaşamamalı.

İkinci önemli sorun şuydu: "Aynı ortamda farklı JetPack sürümleri kurulurken sürüm dosyaları ortak klasörde duruyor; birbirlerine zarar verirler mi?"

Cevap: Orin Stage'in tasarımı target lock digest, base digest, receipt ve workspace ID üzerinden ayrıştırma yaptığı için her sürüm kendi kimliğiyle tutuluyor. Ortak üst klasör kullanılması tek başına sürümlerin birbirini bozduğu anlamına gelmiyor. Önemli olan artifact, base ve workspace'in içerik adresli ve digest kontrollü olması. Bu doğrulamada JP6.0 için yeni base/workspace oluşturulurken JP6.2.3 acquisition ve base korunabildi; sadece disk alanı için kullanıcı onayıyla eski workspace kaldırıldı.

Üçüncü ve en kritik sorun şuydu: "Her base oluştururken böyle küçük küçük yama mı yapacağız? Kullanıcıların başka bilgisayarlarında farklı hatalar çıkacaksa bu şekilde ilerlemek olmaz."

Cevap: Bu itiraz doğru ve önemliydi. Base oluşturma, en çok sürüme özel davranışın ortaya çıktığı yer. Çünkü bu aşama NVIDIA BSP/rootfs, `apply_binaries`, apt repo, meta-package, preinst/postinst script'leri, paket çakışmaları ve offline ARM64 chroot davranışını aynı anda çalıştırıyor. Bu yüzden en fazla problem burada görünür. Ancak çözüm her seferinde dağınık küçük yama eklemek olmamalı.

## Çıkan Mimari Öneri

Bu oturumdan çıkan ana öneri şu: Orin Stage içinde her JetPack sürümü için açık bir "release construction profile" yaklaşımı olmalı.

Bu profil şunları net şekilde taşımalı:

- SDK Manager sürümü, hedef adı ve arşiv modu
- resmi artifact dosya adları ve checksum değerleri
- seçilecek SDKM component role bilgisi
- kurulacak kesin meta-package sürümü
- izin verilen paket kaldırma listesi
- offline chroot için gereken geçici NVIDIA marker davranışları
- beklenen base digest, recipe digest ve package-set digest
- minimum disk ihtiyacı ve ön kontrol bilgileri

Böyle olursa `ensure` komutu bilinmeyen davranışları kurulum ortasında keşfetmez. Önce profile ve preflight kontrolü yapılır; bilinen JP6.0 davranışları kontrollü şekilde uygulanır, bilinmeyen veya değişmiş NVIDIA davranışı varsa sistem erken ve anlaşılır hata verir.

Kullanıcı deneyimi açısından bir sonraki doğru adım, `ostg target preflight` benzeri bir komutla daha kurulum başlamadan disk alanı, SDKM login/lisans, Podman/QEMU/binfmt, cache durumu ve hedef profil uyumluluğunu kontrol etmektir. Özellikle bu oturumda "No space left on device" hatası yaşandığı için disk kontrolü kullanıcıya erken ve net söylenmelidir.

## Son Durum

JP6.0 için ağır doğrulama başarıyla geçti. Kod tarafında yapılan değişiklikler JP6.0'ın gerçek NVIDIA paket davranışlarını daha doğru modelleyen genel düzeltmeler içeriyor. Bu düzeltmeler başkalarının aynı spesifik hataları yaşama ihtimalini ciddi şekilde azaltır.

Yine de bu tek başına "her bilgisayarda kesin sorunsuz çalışır" garantisi değildir. Kullanıcı bilgisayarında hâlâ disk yetersizliği, bozuk cache, eksik SDKM login/lisans, network/proxy/TLS sorunları, Podman/QEMU/binfmt bozukluğu veya NVIDIA'nın ileride repo içeriğini değiştirmesi gibi dış kaynaklı problemler olabilir. Bu nedenle asıl kalıcı iyileştirme, JP6.0 benzeri her sürüm için profile tabanlı kurulum sözleşmesi ve güçlü preflight kontrolüdür.
