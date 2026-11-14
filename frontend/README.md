# Frontend lokal starten

Voraussetzung: Python 3.

```bash
cd frontend
./start.sh
```

Die Website ist danach unter <http://127.0.0.1:5173/> erreichbar. Mit `Ctrl+C` beendest du den Server. Der lokale Server stellt auch `POST /api/send-to-backend` bereit: Beim Klick auf Submit sendet `app.js` Right Ascension, Declination, Pixel Size und Speed an `sendToBackend` und speichert die zurückgegebene UUID im Browser unter `localStorage.videoUuid`.

Der Globus projiziert die Erdoberfläche anhand der aktuellen UTC-Zeit näherungsweise auf das RA/Dec-System. Die Earth-Textur wird von den Three.js-Beispielassets geladen; dafür ist eine Internetverbindung erforderlich. Ohne Textur bleiben Koordinatengitter und Zielpfeil sichtbar.

`start.sh` startet außerdem den Cache-Cleaner. Er entfernt GIFs aus `middleware/video_cache`, die älter als 30 Minuten sind, und wird zusammen mit dem Server beendet.

Einen anderen Port kannst du so wählen:

```bash
PORT=8080 ./start.sh
```

Der Server bindet nur an `127.0.0.1` und ist damit ausschließlich auf deinem Rechner erreichbar. Änderungen an HTML, CSS und JavaScript werden nach dem Neuladen im Browser sichtbar. Falls der Server bereits läuft, starte ihn nach Änderungen an Python-Dateien neu.
