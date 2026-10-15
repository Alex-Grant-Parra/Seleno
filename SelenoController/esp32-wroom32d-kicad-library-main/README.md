# ESP32-WROOM-32D KiCad Library

This repository contains a custom KiCad schematic symbol and PCB footprint library for the ESP32-WROOM-32D (38-pin development board style).

It is designed for PCB designers and embedded systems developers who want a clean, reusable ESP32 module library for KiCad projects.

---

## ⚙️ Library Contents

### Symbol

* ESP32-WROOM-32D schematic symbol
* Proper pin naming and numbering
* Organized pin layout for clarity

### Footprint

* 38-pin development board style footprint
* 2.54mm through-hole header spacing
* Aligned for standard ESP32 dev board form factor

---

## 📌 Pin Coverage

Includes the main functional pins of the ESP32-WROOM-32D module:

* Power: 3V3, 5V, GND
* Control: EN
* UART: TX, RX
* GPIO pins
* ADC inputs: VP, VN
* Internal flash pins marked as NC (No Connect)

---

## 🚀 How to Use in KiCad

### 1. Download or Clone Repository

```bash
git clone https://github.com/yourusername/esp32-wroom32d-kicad-library.git
```

---

### 2. Add Symbol Library

1. Open KiCad
2. Go to:

   ```
   Preferences → Manage Symbol Libraries
   ```
3. Click Add Library
4. Select the folder:

   ```
   symbols/
   ```
5. Apply changes

---

### 3. Add Footprint Library

1. Go to:

   ```
   Preferences → Manage Footprint Libraries
   ```
2. Click Add Library
3. Select:

   ```
   footprints/
   ```
4. Apply changes

---

### 4. Use in Schematic

1. Open schematic editor
2. Press A (Add Symbol)
3. Search for:

   ```
   WROOM32D
   ```
4. Place the symbol

---

### 5. Assign Footprint

1. Select the symbol
2. Press E (Properties)
3. Assign footprint:

   ```
   WROOM32D_Footprint
   ```

---

### 6. Transfer to PCB

In PCB Editor:

```
Tools → Update PCB from Schematic (F8)
```

---

## ⚠️ Notes

* Internal flash pins (CLK, CMD, SD0–SD3) are marked as NC (No Connect) and should not be used.
* Ensure correct 2.54mm header alignment when placing footprint.
* Always verify pin mapping with your physical ESP32 board before fabrication.

---

## 📜 License

This project is licensed under the MIT License.

---

## 👤 Author

Custom KiCad library for ESP32 PCB development and embedded systems design.
