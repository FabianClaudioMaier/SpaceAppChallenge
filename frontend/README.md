# Frontend lokal starten

Voraussetzung: Python 3.

```bash
cd frontend
./start.sh
```

Die Website ist danach unter <http://127.0.0.1:5173/> erreichbar. Mit `Ctrl+C` beendest du den Server.

Einen anderen Port kannst du so wählen:

```bash
PORT=8080 ./start.sh
```

Der Server bindet nur an `127.0.0.1` und ist damit ausschließlich auf deinem Rechner erreichbar. Änderungen an HTML, CSS und JavaScript werden nach dem Neuladen im Browser sichtbar.
