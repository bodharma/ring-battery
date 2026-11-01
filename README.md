# Ring Battery

A small, local macOS menu-bar app for an Oura Gen3 ring: battery percentage, charging reminders, optional desk breaks, and resting heart-rate check-ins.

**Requires macOS 15 or newer, Bluetooth, and your ring’s existing authentication key.** No Oura account login, cloud service, subscription, Python installation, or ring reset is needed. An existing unencrypted iPhone backup can supply the key during setup.

## Install

1. Download [Apple Silicon](https://github.com/bodharma/ring-battery/releases/latest/download/Ring-Battery-arm64.zip) or [Intel](https://github.com/bodharma/ring-battery/releases/latest/download/Ring-Battery-x86_64.zip). [All releases and checksums](https://github.com/bodharma/ring-battery/releases).
2. Unzip and drag **Ring Battery.app** to **Applications**. Open it.
3. Allow Bluetooth and notifications when macOS asks.
4. Complete **Connect your Oura ring**. The percentage appears in the menu bar after a successful reading.

The app is ad-hoc signed, **not Apple-notarized**. macOS may require approval in **System Settings → Privacy & Security** before the first launch. Follow Apple’s normal app-approval flow; the installer does not disable Gatekeeper or remove quarantine attributes.

## Connect your ring

Choose **Connect ring…** from the menu whenever you need to set up or change a ring.

1. Keep the ring close to the Mac and click **Scan nearby rings**. If it does not advertise, put it on its charger and temporarily pause the phone’s Bluetooth connection.
2. Select your ring. A short identifier distinguishes rings with the same name.
3. Import a **key file**, paste the existing 32-character key, or use **Import iPhone backup…** to select an existing unencrypted Finder backup folder or exported Oura app database.
4. Click **Connect this ring**. The app authenticates and reads its battery before storing the key in your login Keychain.

An already paired Oura ring does not expose its authentication key over Bluetooth. Merely selecting the ring cannot bypass that requirement. If you have neither the existing key nor a supported backup/database, connection cannot be completed yet.

Backup import reads only Oura’s database locally. It does not create a phone backup or upload files. **Encrypted backups are not supported** in this release; do not turn off backup encryption just to use this feature. Import an already extracted key/database instead. Multiple distinct ring keys require selecting the correct key separately.

Setup preserves your ring’s existing app key. It never pairs, factory-resets, rekeys, erases history, changes its clock, or starts history synchronization.

## What it does

| Feature | Frequency / behavior |
| --- | --- |
| Battery percentage | At launch, after wake, every 30 minutes while awake, and **Refresh now** |
| Low-battery alert | At 20% or lower, once until a reading exceeds 30% |
| Unavailable alert | After two hours without a successful reading |
| Desk-break reminders | Optional; check Mac input idle every minute, remind after about 50 minutes; five minutes without input resets the timer |
| Resting check-in | Optional, user-started five-minute session; average heart rate and experimental RMSSD |

**Start at login** uses macOS’s standard login-item service. **Send test notification** checks alert delivery. Notification permissions and Focus settings affect visibility. Quitting the app stops monitoring; a sleeping or powered-off Mac cannot poll the ring.

An unreachable ring is shown as **?**, with the last successful reading in the menu. It may be out of range, busy, or empty; the app cannot confirm a dead battery by Bluetooth alone.

Desk reminders measure computer activity, not physical inactivity. They are off by default and can be enabled from the menu.

## Resting check-ins

Wear the ring, sit comfortably, and keep your hand still for five minutes. Live measurement uses extra ring battery. The previous heart-rate mode is restored afterward, including on normal cancellation. If restoration is unconfirmed after Bluetooth loss or forced termination, the next refresh retries it.

HRV is **experimental** and withheld below 80% coarse signal coverage or 150 consecutive valid beat differences. This filters some missing/duplicate data, but is not a validated artifact-correction method. Daytime BLE timing and the physiological readings have not been clinically validated by this project. No stress diagnosis or automatic strain score is produced. Compare results only under similar resting conditions.

Only the latest check-in is kept locally. Oura’s own scores are not reproduced.

## Privacy

The app makes no network requests at runtime. Ring communication is local Bluetooth; keys stay in macOS Keychain. Selected backup/database files are read locally and never copied into the app or uploaded. The ring identifier, alert preferences, recovery marker, and latest check-in stay on your Mac.

Local state lives in `~/Library/Application Support/RingBattery`. Earlier local installations can migrate their private key after successful authentication; the original key file is left in place. Do not include keys, backups, database exports, or health records in GitHub issues.

## Build from source

Requires macOS 15+, Xcode Command Line Tools, and Python 3.13. The distributable bundles Python, so end users do not need these tools.

```sh
git clone https://github.com/bodharma/ring-battery.git
cd ring-battery
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.txt
.venv/bin/python -m unittest discover -s tests -v
./scripts/build.sh
```

The script prints the app’s location and writes `dist/Ring-Battery-<architecture>.zip` plus a SHA-256 checksum. It verifies signatures, the bundled reader, and Swift self-checks using an empty environment. GitHub Actions builds Apple Silicon and Intel packages separately on macOS 15 runners. A `v*` tag publishes verified release assets after both builds pass.

Source layout: `Sources/RingBattery/main.swift` is the native app; `backend/read_ring.py` handles authenticated BLE and local key import; `tests/` contains synthetic tests; `scripts/build.sh` packages the app.

## Support and license

Gen3 battery access has been tested on real hardware. Other ring generations are **not verified**. This project is independent of Oura and is not a medical device.

MIT licensed. BLE protocol helpers are adapted from [Th0rgal/open_oura](https://github.com/Th0rgal/open_oura), pinned in [UPSTREAM.txt](backend/vendor/UPSTREAM.txt), with the original [MIT license](backend/vendor/LICENSE). Bundled dependency licenses are included in the app’s Resources folder.
