# 🔄 Retro Tool

Ein einfaches, live-fähiges Retrospektiven-Tool für Teams bis 50+ Personen.

## Features

- ✅ Admin-Bereich mit QR-Code und Phase-Steuerung
- ✅ Mobile-optimierte Teilnehmer-Ansicht
- ✅ Live-Updates via WebSocket (kein Page-Reload nötig)
- ✅ Phasen: Lobby → Eingabe → Moderation → Voting → Auswertung
- ✅ Dot-Voting (5 Dots pro Person)
- ✅ Karten zusammenführen & löschen (Moderation)
- ✅ Ergebnis-Ranking nach Votes

---

## Setup

### 1. Voraussetzungen
- Python 3.10+ installiert

### 2. Abhängigkeiten installieren

```bash
cd retro
pip install -r requirements.txt
```

### 3. Server starten

```bash
python main.py
```

Der Server läuft dann auf: http://localhost:8000

---

## Verwendung

### Als Admin
1. Browser öffnen: http://localhost:8000
2. Session-Name eingeben → "Erstellen"
3. QR-Code erscheint → Teilnehmer scannen lassen
4. Phasen steuern mit den Buttons

### Als Teilnehmer
1. QR-Code mit Handy scannen
2. Karten eingeben (Keep / Stop / Improve)
3. Nach Moderation: Dots vergeben (5 pro Person)
4. Ergebnisse anschauen

---

## Projektstruktur

```
retro/
├── main.py              # FastAPI Backend + WebSocket + SQLite
├── requirements.txt
├── retro.db             # SQLite DB (wird automatisch erstellt)
├── templates/
│   ├── admin.html       # Admin-Interface
│   └── participant.html # Mobile Teilnehmer-Interface
└── static/              # (für zukünftige CSS/JS-Dateien)
```

---

## Phasen erklärt

| Phase | Was passiert |
|---|---|
| **Lobby** | Teilnehmer verbinden sich via QR-Code |
| **Eingabe** | Jeder gibt Keep / Stop / Improve Karten ein |
| **Moderation** | Admin löscht Duplikate, führt ähnliche Karten zusammen |
| **Voting** | Jeder verteilt 5 Dots auf Stop + Improve Karten |
| **Auswertung** | Ranking nach Votes, sichtbar für alle |

---

## Nächste Schritte / Erweiterungsideen

- [ ] Export als PDF/CSV
- [ ] Mehrere Sessions gleichzeitig (bereits unterstützt)
- [ ] Timer pro Phase
- [ ] Action Items nach der Retro erfassen
- [ ] Passwortschutz für Admin

---

## Deployment auf Server

Für echten Server-Betrieb:
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Dann QR-Code URL auf die echte Server-IP setzen (Base-URL wird automatisch aus dem Browser-Origin genommen).
