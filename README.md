# Parking Update Visualizer

Live map of the parking/traffic-sign survey: **https://sts-labs.github.io/Parking-Update-Visualizer/**

It shows the route the car has covered, every geolocated sign, and a popup per sign with the detected frame, confidence and Google Maps / Street View links.

## How it updates

1. The detection laptop uploads checkpoints to the public Drive folder
   `STS-Labs-ZED-outputs/<date>/<session>/<checkpoint_NNN|final>/`, and every review photo once to
   `STS-Labs-ZED-outputs/<date>/<session>/photos/`. Each checkpoint lists its photos' Drive ids in
   `*_photo_ids.json` and goes up with its `*_progress.json` last.
2. Every 15 minutes the GitHub Action [`sync-and-deploy`](.github/workflows/sync-and-deploy.yml) runs
   [`scripts/sync.py`](scripts/sync.py). It crawls the Drive folder without credentials (the folder must stay shared as
   "anyone with the link"), takes the newest *complete* checkpoint of each session (or `final`): one still uploading
   has no `progress.json` yet and is skipped. It writes:
   - `data/index.json`: sessions, progress and bounding boxes
   - `data/sessions/<id>.geojson`: sign points and the GNSS track (converted from the shapefile)
3. If anything changed, the data is committed and the site is redeployed. Open pages re-check for new data every 2 minutes.

Photos are not copied: popups load them straight from Drive thumbnails. Checkpoints up to `checkpoint_019` of
`session_2026-09-30_14-10-32` still carry their own `*_photos/` folder; `sync.py` reads both layouts.

What the map leaves out or marks:
- Signs 50 m or more from the GNSS track are dropped (georeferencing errors; see `FAR_FROM_TRACK_M` in `sync.py`).
- Signs in a GNSS gap (`gnss_gap` from the pipeline: no usable fix around them) can't be checked against the
  track. They are kept, drawn as hollow dashed rings, and their popup says the position comes from camera tracking only.
- The route line is broken where the GNSS log has gaps longer than 10 s, instead of a straight line across them.

Click **Actions → Sync Drive & deploy map → Run workflow** to force an update immediately.

### Trigger

GitHub's own `schedule` trigger turned out to be unreliable (it never fired for this repo), so the detection laptop
starts the workflow itself. A Windows Task Scheduler job named **ParkingMapSync** runs every 15 minutes:

```
wsl.exe -d Ubuntu -u zuka -e /home/zuka/.local/bin/gh workflow run sync-and-deploy.yml -R STS-Labs/Parking-Update-Visualizer
```

It needs the laptop on and `gh` logged in inside WSL. To remove it, run `schtasks /Delete /TN ParkingMapSync /F`.
The cron schedule in the workflow stays as a backup.

## Local development

```bash
pip install -r requirements.txt
python scripts/sync.py                 # pull from Drive into data/
python scripts/sync.py --local DIR     # or use a local folder with the same layout
python -m http.server 8000             # open http://localhost:8000
```

To point at a different Drive folder, set `DRIVE_ROOT_ID`, or pass `--root <folder id>`.
Sign-code names live in [`signs.js`](signs.js).
