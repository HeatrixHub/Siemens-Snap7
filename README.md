# Siemens-Snap7

Ein minimales Beispielprojekt zum gleichzeitigen Auslesen zweier Siemens-SPS
über python-snap7 und Anzeige der Messwerte in einem Web-Dashboard.

## Voraussetzungen

- Python 3.10+
- Abhängigkeiten: `pip install flask python-snap7`
- Netzwerkzugriff auf die beiden SPS (Rack 0, Slot 1)

## Starten

1. Trage die IP-Adressen sowie die gewünschten DB-Nummern und Offsets in
   `plc_dashboard.py` im `plc_config`-Block ein.
2. Starte die Anwendung:

   ```bash
   python plc_dashboard.py
   ```

3. Öffne `http://localhost:5000` im Browser, wähle die gewünschten Signale aus
   und beobachte die Live-Graphen.
