# Проверка аппаратной совместимости ZBT WE1626 (14.11.2026)

Источники: скриншоты владельца с read-only команд Keenetic NDMS 2.08 (Telnet). Секреты/пароли и серийные номера не сохранены.

## Фактическая работающая конфигурация

- `show version`: `release v2.08(AAUS.4)C2`; аппаратная идентификация внутри NDMS: `hw_id=kn_rf`, `hw_version=12132000-F`, `device=Keenetic Omni II`. **Это идентификация ПО, не подтверждение физической платы Omni II.**
- `show usb`: `CdcEthernet0`, `ehci-platform/usb1`, USB `2.00`, путь заканчивается `net/eth0`. Контроллер USB/EHCI и сетевой режим модема **работают сейчас**. Имя интерфейса `eth0` в NDMS нельзя переносить в OpenWrt без проверки.
- `more proc:/cpuinfo`: `MediaTek MT7620 SoC`, MIPS 24KEc, ~385 BogoMIPS; не отличает MT7620A от MT7620N.
- `more proc:/cmdline`: `console=ttyS0,57600n8 rdinit=/sbin/init`; это параметры ядра NDMS, **не доказательство baudrate загрузчика**.
- `more proc:/mtd`: SPI NOR 8 MiB; `U-Boot` 0x30000, `U-Config` 0x10000, `RF-EEPROM` 0x10000, `Firmware` 0x720000, `Config` 0x10000, `Storage` 0x80000. `Firmware` начинается по 0x50000 и заканчивается по 0x770000.

## Несовпадения с доступными профилями

- OpenWrt `zyxel_keenetic-omni-ii`: `firmware` 0x7B0000 начиная с 0x50000, граница 0x800000. Это перекрывает Config и Storage на 0x90000 = 576 KiB.
- OpenWrt `zbtlink_zbt-we2026`: `firmware` 0x760000 начиная с 0x50000, граница 0x7B0000. Это перекрывает 0x40000 = 256 KiB.
- Обе официальные DTS этих профилей используют `mt7620n.dtsi`; по информации производителя ZBT для WE1626 существуют противоречивые каталожные сведения MT7620A / MT7620N. Требуется подтверждение точной микросхемы.
- У WE2026 профиль не содержит явного включения EHCI/OHCI, в отличие от Omni II. В прошивке нужен работающий ZTE CDC Ethernet.
- `factory.bin` для Omni II имеет правильный контейнер и размер, но **не подтверждён как первый флеш на физической плате WE1626**.

## Что ещё получить

1. Чёткое фото платы сверху с читаемой маркировкой CPU и флеш-чипа и обозначением PCB/revision. Желательно фото второй стороны платы.
2. Для загрузчика: снимок UART-лога загрузки, если возможен; не вводить команды записи flash. Отладочную пайку и подключение доверить специалисту.
3. В идеале безопасный 8MiB SPI NOR dump у специалиста: обязательно сохранить разделы U-Boot и RF-EEPROM до рискованной записи.

**Текущий статус: старые бинарники не прошивать, сначала подтвердить SoC, GPIO/Ethernet/USB, реальную разметку и безопасный first-install путь.** Нужен новый индивидуальный DTS и новый тип factory-image, а не простая замена имени файла.

Источники аппаратных профилей:
- https://github.com/openwrt/openwrt/blob/openwrt-25.12/target/linux/ramips/dts/mt7620n_zyxel_keenetic-omni-ii.dts
- https://github.com/openwrt/openwrt/blob/openwrt-25.12/target/linux/ramips/dts/mt7620n_zbtlink_zbt-we2026.dts
