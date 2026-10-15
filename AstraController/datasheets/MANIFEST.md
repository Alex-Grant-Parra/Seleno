# Datasheet Manifest – ASTRA Controller

Downloaded 2026-10-13 for the pre-PCB schematic review.

Added 2026-10-15 for the TPS5430 + AMS1117 power change: TPS5430, AMS1117 and the Changjiang FXL inductor series. Same process: each was quarantined, its header checked as `%PDF-`, and its objects scanned with PyMuPDF for the keys below plus `/OpenAction` and `/AA` (none found, no embedded files, not encrypted). A Windows Defender custom scan found no threats.

## Safety process
1. **Source:** Each file comes from the manufacturer's own domain over HTTPS where possible. Where the manufacturer blocks automated downloads, the copy comes from a major distributor (DigiKey, RS) or a reputable maker vendor (SparkFun). The table lists the source tier for every file.
2. **Quarantine:** Files were first downloaded into a scratch folder outside the project.
3. **Type check:** Every file's first bytes are `%PDF-`. Files that came back as HTML (block or redirect pages) were deleted. No executable headers were found.
4. **Static PDF scan:** Raw bytes and decompressed streams were searched for active content: `/JavaScript`, `/JS`, `/Launch`, `/EmbeddedFile`, `/RichMedia`, `/XFA`, `/SubmitForm`, `/ImportData` and `/GoToE`.
   - None were found as real PDF keys.
   - SPX3819 had two `/JS` byte matches. On inspection, both are random bytes inside compressed binary stream data, so they are false positives.
   - `/OpenAction` entries were checked: they are plain "GoTo page / Fit view" actions.
5. **Antivirus:** A Windows Defender custom scan (MpCmdRun, platform 4.18.26080.4) of the quarantine folder found no threats.
6. **Integrity:** SHA-256 hashes of the files in this folder were confirmed to match the scanned copies.

Tip: open these in a viewer with JavaScript disabled, e.g. Edge or SumatraPDF. All of them scan clean, so this is just good practice.

## Files

| File | Covers (refs) | Source | Tier | SHA-256 |
|---|---|---|---|---|
| ESP32-WROOM-32E_32UE_datasheet.pdf | U5 | documentation.espressif.com | Official | `4c7a345d1c1bfec34c38665639e39a7f43b79a35a12f6adcc2c7c0f83850f8b8` |
| ESP32_chip_datasheet.pdf | U5 (SoC: strapping pins, GPIO) | documentation.espressif.com | Official | `a7917e6b47528c9dcab06837a49d452e582751335797db879f1cf2d17cd29adf` |
| ESP32_hardware_design_guidelines.pdf | U5 (reference schematic and layout) | documentation.espressif.com | Official | `a6da9c7062938ad0a8fb2305cacf78802878e7df523d71c2986836312306aa43` |
| TMC2209_datasheet.pdf (rev 1.08) | U7, U8 | media.digikey.com (analog.com blocks scripts) | Distributor | `24d6fe363ff4e5898c0c3fa2ed06e9d8a04e34481a5ad5cc9e4555e7403fdd44` |
| USB2512B_USB251xB_datasheet.pdf (DS00001692C) | U2 | ww1.microchip.com | Official | `3fca1a17701a3fd513c867f4deab71887b317302ff7cbbc22310c1ddaa6bdaa9` |
| MCP23017_datasheet.pdf | U1 | ww1.microchip.com | Official | `63cb5f2bec44434cdeeada1790d0316c9dc06b33febb489ad87bb0e2d540496a` |
| CH340_datasheet.pdf (WCH CH340DS1) | U3 | cdn.sparkfun.com (WCH site has no direct link) | Vendor mirror | `04c805e8242885fd1cf21f05dbfd9d16b9fa38f0439ce0d3c6d7f74ebe4cf4af` |
| SPX3819_datasheet.pdf | U4 | maxlinear.com | Official | `43b63129331150a20339623920ea9f7be758d93e700a3459beb190d483ad35a2` |
| AP62300_datasheet.pdf | U6, U9 until 2026-10-15 (replaced by the TPS5430 + AMS1117) | diodes.com | Official | `a9c6bcf391ba2df0e2e81cd1c881f579b4fdf000e9ba87073920cb4ffba1e1cc` |
| AO3401A_datasheet.pdf | Q1 (current part) | aosmd.com | Official | `0d8e3261ae280e007b5837fff60c55142f2d92af71edc4d02aee6f7a760a785c` |
| AO3407A_datasheet.pdf | Q1 (proposed ±20 V Vgs replacement) | aosmd.com | Official | `aff0ea069d574266a52bb479a734d753531940c773accdb3c43605a01d8de6df` |
| MMBT3904_datasheet.pdf (onsemi) | Q2, Q3 (proposed SMD replacement for 2N3904) | docs.rs-online.com (onsemi blocks scripts) | Distributor | `97d19af406d5445cb06862cb018dd23196824a9ac1d894015bb792295a96c2fb` |
| BZT52C_zener_series.pdf | D6 (representative; no part chosen yet) | diodes.com | Official | `0fbd7d137524820f0160594065cbceb8c6b3188757c328416d08fceb7418202f` |
| BNO080_085_datasheet.pdf | G1 | ceva-ip.com | Official | `34ac0ca1dda8fb0ecf347b35042bb32d22c953b127a0e414852213edc2c502ca` |
| ST7735S_datasheet.pdf (v1.1) | S1 (LCD module controller) | cdn.sparkfun.com (Sitronix does not publish) | Vendor mirror | `0322a5279c9c0ee967c645eac505a727db97dec7ea0546588eb508dcbbbc92b7` |
| CUI_UJ2-ADH-TH_USB-A_stacked.pdf | J6 | sameskydevices.com (formerly CUI Devices) | Official | `6630022bb721f05f76efe943960147acea8b632e54d36e03031ea9a4f34cda36` |
| JST_PH_connector.pdf | J1 (current footprint) | jst-mfg.com | Official | `447624f4f2f7d37c58c1eaa7ee314ad757fe7aff48f6186491ef6f69fbc00b96` |
| JST_XH_connector.pdf | J1, J2 (proposed motor connector) | jst-mfg.com | Official | `9426b136902f11900825077535e5c65032b7fbc31ffb59c5e9e1f463bb20fb90` |
| JST_SH_connector.pdf | J3, J4, S1, SW1–SW7 | jst-mfg.com | Official | `ea3071ca5ee5a6069eba534fa39a10f34c9fb742ee42135a69ab4156bfa0f5de` |
| JST_VH_connector.pdf | P1 | jst-mfg.com | Official | `d51e669c597988b20c0963daf5bef7356cbd2104c1f867e9107c6fa6cd2b899c` |
| TPS5430_datasheet.pdf (SLVS632L, rev. Apr 2026) | U6 (5 V buck, added 2026-10-15) | ti.com | Official | `88c98e6e51851285fbbfc17837d002b29af7cd3fe30442fa7e4612c5179985da` |
| AMS1117_datasheet.pdf | U9 (3.3 V LDO, added 2026-10-15) | advanced-monolithic.com | Official | `189a2651878a87d590b768eaa9b44217a3fdf460352ce6ecaff127221282a3f0` |
| Changjiang_FXL_inductor_series.pdf | L2 (FXL0840-150-M, added 2026-10-15) | atta.szlcsc.com (LCSC) | Distributor | `d41cba259fbe60f80e20eb7d28672008846b08040c69edadc8af46d8cd59d94e` |

## Not downloaded
| Part | Reason |
|---|---|
| L2 Würth 7447789003 | we-online.com returns 403 to all automated downloads. The inductor value is still undecided (see review). Download manually from the Würth site once the part is chosen. |
| Q2/Q3 2N3904 (TO-92) | onsemi returns 403. The review recommends replacing it with the MMBT3904 (included above). |
| F1 polyfuse, Y1 24 MHz crystal | No part number has been chosen yet, so there is nothing specific to download. |
| Passives (R, C, LEDs) | Generic, with no part numbers. Datasheets will be added once specific parts are chosen (e.g. JLCPCB basic parts). |
