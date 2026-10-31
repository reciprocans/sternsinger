# Sternsinger Verwaltung v66 – Portainer

v63 ist ein vollständiger UI/UX-Refresh auf Basis von v60. Datenbank, Rollen, Jahresaktionen, Spenden, Straßen, Karten, SMTP, Datenschutz-Werkzeuge und vorhandene Docker-Volumes bleiben kompatibel.

## Neu in v66

- neue ungeschnittene Bannerbilder des Nutzers eingebunden
- `bright.png` wird im Hellmodus verwendet
- `dark.png` wird im Dunkelmodus verwendet
- Bilder werden unverändert und proportional dargestellt; kein Crop und keine Bildbearbeitung


## Neu in v63

- neue UX-/Navigationsschicht unter `static/app-v63.css` auf der bestehenden Designbasis
- ruhiger, warmer Light-Mode und konsistenter Dark-Mode
- klare linke Desktop-Navigation mit aktiven Zuständen, Zählern und einklappbarer Icon-Leiste
- konsistente Header-Aktionen inklusive Schnellzugriff „Neue Person“
- vierteilige Mobile-Bottom-Navigation für Verwaltung/Superadmin
- rollenangepasste Mobile-Navigation für die Spendenrolle
- größeres, touchfreundliches Mobile-Menü als Bottom-Sheet
- einheitliche Buttons, Formulare, Tabellen, Karten, Karten-/Statusflächen und Dialoge
- moderne Fokuszustände und bessere Tastaturbedienung; Escape schließt das mobile Menü
- kompakte Sprungnavigation innerhalb der Einstellungen
- responsive Formulare und Tabellen ohne unnötiges Zoomen auf Smartphones
- Login, Passwort-Seiten, Datenschutz und Gruppenkarte optisch an die Hauptanwendung angeglichen
- die Spenden-Liveansicht bleibt bewusst unverändert im festen v33-Stil

## Banner

Header- und Footer-Banner liegen normal im Dokumentfluss. Die aktiven Bannerdateien sind 1500 × 150 Pixel groß und werden proportional dargestellt. Die Einstellung unterscheidet weiterhin zwischen kleiner und großer Darstellung; der Footer darf mehr Raum einnehmen.

## Update

Das vorhandene Docker-Volume kann unverändert weiterverwendet werden. Vor einem Update empfiehlt sich ein Backup des Volumes bzw. ein Export aus der Anwendung.

## Start

```bash
docker compose up -d --build
```

Standard-Port: `8088`.

## Datenschutz

v63 übernimmt die Privacy-by-Design-Werkzeuge der vorherigen Versionen. Das ist keine rechtliche Zertifizierung. Verantwortliche Stelle, Rechtsgrundlagen, Informationspflichten, Aufbewahrungsfristen und organisatorische Maßnahmen müssen weiterhin zum konkreten Einsatz passen.


## v63 – Navigations- und UX-Redesign
- Navigation neu gegliedert in Einsatz, Menschen, Finanzen und System.
- Mobile Hauptnavigation: Gruppen, Personen, Neu, Menü; Spendenrolle mit eigener Navigation.
- Ruhigere, konsistentere Karten-, Formular- und Tabellenhierarchie.
- Verbesserte Fokus-, Hover- und aktive Zustände sowie kompaktere mobile Bedienung.


## v63 Aufräumen
- Ungeschnittene Hell-/Dunkel-Originalgrafiken wiederhergestellt.
- Keine Banner-/Designauswahl mehr.
- Stylesheets je Oberfläche konsolidiert: app.css, auth.css, privacy.css, map.css.
- Alte versionsgebundene CSS-Schichten entfernt.


## v63 – Druckcenter

- Gruppen-Einsatzblatt neu als DIN A4 quer, eine Seite pro Gruppe.
- Ernährung, Allergien/Unverträglichkeiten und Hinweise direkt je Sternsinger sichtbar.
- Route, Hausanmeldungs-Anzahl, Termine sowie Start/Rückkehr/Spende kompakt auf demselben Blatt.
- Einzelne Personenlisten für Sternsinger, Begleiter, Mittagessen, Helfer und alle Personen im Druckcenter.


## Neu in v66
- Liveanzeige: Meilensteine deutlich feierlicher inszeniert.
- 1.000er mit stärkerem Stern-/Konfettijubel.
- 5.000er mit Goldstrahlen, Krone/Sparkle, Konfettiwellen und Feuerwerk.
- 10.000er mit Finale-Stufe, zusätzlichen Feuerwerken und längerer Celebration.
- Kein Ton; Liveansicht bleibt im festen dunklen v33-Stil.


## v66 – Excel-Export für Serienbriefe
- Druckcenter: neuer Excel-Export mit getrennten Tabellenblättern für Sternsinger, Begleiter, Mittagessen, Helfer und Alle Personen.
- Mailmerge-taugliche Spalten für Namen, Adresse, Telefon, Gruppe sowie kategoriespezifische Zusatzfelder.
- Export nur für Verwaltung und Superadmin.
