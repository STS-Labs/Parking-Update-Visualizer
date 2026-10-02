# Parking Update Visualizer

Live map of the parking/traffic-sign survey: **https://sts-labs.github.io/Parking-Update-Visualizer/**

It shows the route the car has covered, every geolocated sign, and a popup per sign with the detected frame, confidence and Google Maps / Street View links.

## How it updates

1. The detection laptop uploads checkpoints to the public Drive folder
   `STS-Labs-ZED-outputs/<date>/<session>/<checkpoint_NNN|final>/`.
2. Every 15 minutes the GitHub Action [`sync-and-deploy`](.github/workflows/sync-and-deploy.yml) runs
   [`scripts/sync.py`](scripts/sync.py). It crawls the Drive folder without credentials (the folder must stay shared as
   "anyone with the link"), takes the newest checkpoint of each session (or `final`), and writes:
   - `data/index.json`: sessions, progress and bounding boxes
   - `data/sessions/<id>.geojson`: sign points and the GNSS track (converted from the shapefile)
3. If anything changed, the data is committed and the site is redeployed. Open pages re-check for new data every 2 minutes.

Photos are not copied: popups load them straight from Drive thumbnails.

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
