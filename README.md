# Belgium Flight Heatmap

A free, public map of where planes from **Brussels Airport** fly over, made for
house hunters. It shows a heatmap of past flight tracks and of the flights you can
expect in future, and gives any address an **overflight score from 0 to 10**.

- **Free to run:** a static website on GitHub Pages; data is refreshed daily by a
  GitHub Action. No server, no paid hosting.
- **Works on your phone:** open the site and choose *Add to Home Screen*
  (Safari: Share → Add to Home Screen; Chrome: ⋮ → Install app). It then opens
  like an app and keeps working with a weak connection.

## What you see

| Layer | Source | Use it for |
|---|---|---|
| Expected: typical year | Model of runway use and routes | The general picture for an address |
| Expected: westerly / easterly wind, weekend & night | Model, one operating mode each | How an address changes when the wind turns |
| Observed: last 30 / 90 / 365 days | Real ADS-B tracks from the OpenSky Network | What actually happened |

Filters: altitude band (under 600 m, 600 to 1,500 m, 1,500 to 3,000 m) and day or
night (night = 23:00 to 06:00). Tap the map or search an address for its score
card. The URL updates as you go, so you can share a link to an exact spot.

**The score** counts flights per day passing within about 250 m, weighted by
altitude (low ×1, medium ×0.35, high ×0.1), with night flights counted ten times
(like the official L<sub>den</sub> measure). 300 weighted flights per day or more
is a 10. It is an indicator, not a noise measurement.

## Setup (one time)

1. **Enable GitHub Pages:** repo *Settings → Pages → Build and deployment →
   Deploy from a branch → `main` / `(root)`*. The site appears at
   `https://<your-user>.github.io/<repo>/`.
2. **Real flight data (optional, free):**
   1. Create an account at <https://opensky-network.org>.
   2. In your OpenSky account page, create an **API client** and copy its client
      id and secret.
   3. In this repo: *Settings → Secrets and variables → Actions → New repository
      secret*: add `OPENSKY_CLIENT_ID` and `OPENSKY_CLIENT_SECRET`.
   4. *Actions → Update flight data → Run workflow* to start right away. After
      that it runs every morning, backfilling about the last 25 days and then
      adding one day at a time. Older data builds up from there (OpenSky only
      keeps detailed tracks for about a month, so history starts when you start).

Without the OpenSky secrets the site still works with the expected (modelled)
layers.

## Run locally

```sh
python3 scripts/build_modelled.py          # rebuild expected layers
OPENSKY_CLIENT_ID=... OPENSKY_CLIENT_SECRET=... python3 scripts/fetch_opensky.py
python3 -m http.server                     # then open http://localhost:8000
```

## Tuning the model

Everything the expected layers assume lives at the top of
[`scripts/build_modelled.py`](scripts/build_modelled.py): runway positions, flights
per day, night share, how often each wind configuration is in use, and the
departure route fans. Once a few months of observed data exist,
`data/stats.json` shows the real runway split per day, which you can copy into
`CONFIGS` to calibrate the model.

## Limits

- The model is approximate. Real departure routes and the runway scheme change
  with politics and court rulings; check the official sources before buying.
- ADS-B coverage near the ground can be patchy, so the lowest part of some tracks
  may be missing in observed data.
- Only Brussels Airport (EBBR) is covered for now. Other Belgian airports (Liège,
  Charleroi, Antwerp, Ostend) can be added by repeating the same steps per airport.

## Credits and licences

Code: MIT. Flight data © [OpenSky Network](https://opensky-network.org), for
non-commercial use under its terms. Base map © OpenStreetMap contributors, © CARTO.
Address search: Nominatim / OpenStreetMap. Map library: Leaflet (BSD-2).
