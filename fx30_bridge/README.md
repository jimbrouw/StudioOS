# FX30 Bridge

This is the small HTTP layer that Companion can call.

## Current Studio Recording Direction - 9 July 2026

The studio now also has an experimental three-camera NDI recording path in OBS:

```text
FX30 A/B/C
  -> clean 4K HDMI
  -> ZowieBox NDI encoders
  -> dedicated gigabit network
  -> OBS on the Mac mini M4 Pro
  -> separate Source Record camera files
```

OBS has been configured with:

- Exeldro Source Record for separate camera recordings in the three-camera
  collection. The current `1_cam_ndi` collection deliberately has its Source
  Record filter removed because version 0.4.8 left an OBS output alive and
  blocked shutdown in `obs_output_destroy()`.
- `hybrid_mp4` containers.
- Apple VT H.264 hardware encoding.
- A current test target of 30.00 fps.
- A current test bitrate range of 35,000-50,000 Kbps.
- Automatic file splitting disabled.
- Audio Stems Recorder set for 24-bit/48 kHz WAV files.
- A provisional 50-150 ms positive audio delay to compensate for NDI picture latency.

These settings are **configured, not yet production-proven**. Before relying on
them, complete a three-camera 30-minute recording, Premiere import, dropped-frame
check, audio clap test, and forced-quit recovery test. Test 30.00 fps under the
actual US studio lights; use the measured result to decide whether 30.00 or
29.97 fps is correct.

The current one-camera mitigation records the normal OBS program output at
3840x2160 HEVC instead of creating a redundant Source Record output. Audio
Stems Recorder writes its WAV sessions under
`/Volumes/Extreme SSD/TEST-RECS/OBS-MASTERS/AUDIO-STEMS`. The launcher supplies
`/opt/homebrew/bin` in OBS's `PATH` so the plugin can run FFmpeg and finish its
configured 24-bit conversion.

When the guarded post-record delivery is enabled, completed Audio Stems sessions are copied recursively
to `/Volumes/specialized2026/studio/Studio Recordings/OBS Audio Stems`. Timestamped session
folders and sidecar JSON files are preserved. Files newer than
`stableFileSeconds` are skipped until the next copy, and an existing same-size
destination is skipped safely.

`COPY FILES` now finishes with the RØDECaster stage as well. It selects the
connected RØDECaster Pro II in the official app, enters Transfer Mode, selects
the newest take, verifies it to the external SSD and LucidLink with SHA-256
receipts, then exits Transfer Mode so USB audio/MIDI returns automatically.
Camera and OBS delivery still runs independently, and the dashboard reports a
combined failure if the RØDECaster stage needs attention.

Automatic post-stop ingest is disabled with
`autoIngestAfterStopSeconds: 0` in `config.json`. Companion **STOP + DELIVER**
now owns the sequence through
`/Users/jim/Developer/build/strict_studio_stop_deliver.command`, preventing a
second RØDECaster transfer from racing the visible delivery action. The bridge
can still schedule its former automatic COPY FILES sequence if this value is
set above `0`.

### Remaining OBS and audio work

1. Confirm whether OBS receives the RØDECaster Pro II as grouped stereo or as
   usable separate inputs on this exact Mac.
2. Choose one isolation method:
   - preferred simple route: record multitrack WAV files to the RØDECaster
     microSD card and sync them in Premiere using scratch audio;
   - paid software route: use Rogue Amoeba Loopback;
   - free/manual software experiment: use BlackHole.
3. Build a separate experimental Companion page that starts and stops the OBS
   master recording, all Source Record filters, and Audio Stems Recorder.
4. Add visible Companion feedback for OBS state, missing NDI sources, and
   completed files.
5. Keep the existing FX30 internal-card and SDK ingest workflow available until
   the OBS route passes the complete proof test.

### RØDECaster recording ingest

The RØDECaster App has no documented command-line transfer API. On this Mac,
USB 1 Transfer Mode exposes the card as `/Volumes/RodeCaster`, so the physical
mode change remains manual and the file delivery can be scripted.

After recording has stopped and finalised, the supported manual route is:

1. In RØDECaster App, choose **Transfer Recordings** and continue into Transfer
   Mode.
2. Run:

   ```text
   /Users/jim/Developer/build/fx30_bridge/ingest_rodecaster.command
   ```

The `.command` wrapper runs `rodecaster_ingest.py --wait-seconds 60`. The
Python command can also be used directly when an exact take or alternate test
destination is required.

For the experimental one-click route, close OBS after the recording is safely
stopped and run:

```text
/Users/jim/Developer/build/fx30_bridge/transfer_and_ingest_rodecaster.command
```

This opens the official RØDECaster App and uses the local
`rodecaster_select_device` accessibility helper to select the connected
**RØDECaster Pro II** tile, press **Transfer Recordings**, press **Continue**,
wait up to 60 seconds for `/Volumes/RodeCaster`, and then run the same verified
ingest. The **Continue** warning is expected. If the card is already mounted,
the UI steps are skipped safely. Companion prevents overlapping STOP + DELIVER
workers so two transfers cannot fight over the same device. The first run may
require the operator to grant Accessibility permission when macOS prompts. The
wrapper refuses to switch modes while an `obs` process is running. After a
successful or failed ingest, it presses **Back**, waits for the card to unmount,
and returns the RØDECaster to normal USB audio/MIDI operation.

RØDE does not document this UI as an automation API, so keep
`ingest_rodecaster.command` as the supported fallback. Re-test the one-click
route after every RØDECaster App update because button names or hierarchy may
change.

The script selects the newest take unless `--take "Exact folder name"` is
provided. It refuses symbolic links and changed/incomplete source files, never
overwrites a different existing take, copies through a verified temporary
folder, and confirms matching file sizes and SHA-256 hashes at all three
locations:

```text
RØDECaster card
  -> /Volumes/Extreme SSD/TEST-RECS/AUDIO/RODECASTER
  -> /Volumes/specialized2026/studio/Studio Recordings/RØDECaster Audio
```

JSON checksum receipts are written below `_receipts` in both destination
roots. Re-running the same take is safe: matching copies are verified and
reported as `already_verified`; a collision with different content is a hard
failure.

Phase one runs on the Mac mini. The Raspberry Pi version is phase two.

It does not replace the Sony Camera Remote SDK. It runs configured local commands such as:

```text
sony-rec --camera fx30-a --action start
sony-rec --camera fx30-a --action stop
sony-rec --camera fx30-a --action ingest-proxy --dest /Users/jim/Movies/FX30_A
sony-rec --camera fx30-a --action status
```

Those commands should be implemented with the Sony SDK sample in `../RemoteCli`.

The native FTP watcher is retained as an experimental option but is disabled in
the current studio config. Physical testing showed that the FX30's required
all-Wi-Fi setup cannot keep PC Remote control and native FTP active together.
The studio therefore keeps FTP off for unattended operation.

`COPY FILES` uses Sony SDK Contents Transfer mode and asks `sony-rec` to pull
the latest proxy candidate. The helper prints every SDK-exposed movie candidate
and selects the newest 1080/HD proxy file when the camera exposes both proxy and
original for the same clip.

`TEST FTP` is deliberately read-only. While PC Remote is connected, it asks the
camera which FTP properties are available and whether Sony marks them writable.
It does not change modes. Read the `FTP_PROPERTY` lines in the studio command
window before adding any command that could disconnect PC Remote.

## First End-to-End Test

1. Confirm the external SSD and LucidLink are mounted.
2. Turn on one FX30 and leave only `fx30-a` enabled in `config.json`.
3. Open `/Users/jim/Developer/build/start_fx30_studio.command`.
4. Press the dashboard or Companion Start button and record for 20 seconds.
5. Press Stop once.
6. Press `COPY FILES`.
7. Watch the dashboard. It should show the SDK proxy filename, size, copy
   progress, and then `Delivered ... to LucidLink`.
8. Confirm the proxy appears in `TEST-RECS/FX30_A`, then in the LucidLink
   `FX30_A` folder, followed by the completion email.

Restart `start_fx30_studio.command` after changing `config.json`; the bridge
loads this file only at startup.

## Manual Emergency 4K Master Recovery

The dashboard has separate **EMERGENCY: CAMERA 1/2/3 MASTER** buttons for an NDI
master failure. Each button pulls only the selected camera's newest 4K master
into that camera's local Extreme SSD folder. Disabled cameras remain visibly
unavailable. Recovery is deliberately separate from normal stop, proxy copy,
LucidLink delivery, and email. It never runs automatically.

The action:

1. refuses to run while camera recording state is active or changing;
2. releases the warm Sony control connection and enters Contents Transfer mode;
3. selects the newest 3840x2160 non-proxy movie and refuses an HD proxy
   substitute;
4. copies it into the configured `masterIngestFolder` on the Extreme SSD;
5. verifies non-zero duration, 3840x2160 resolution, size, and SHA-256;
6. leaves the camera-card original in place and restores the warm connection.

Current one-camera test destination:

```text
/Volumes/Extreme SSD/FX30-EMERGENCY-MASTERS-2026-08-13/FX30_A
```

Live proof on 13 August 2026 recovered `C2008.MP4`: 11.04 seconds,
3840x2160/25p H.264 4:2:0, stereo 48 kHz PCM, 134,363,919 bytes. This proves
the short one-camera recovery path, not long-record transfer endurance or a
three-camera simultaneous recovery.

The example config defines three camera slots but only enables `fx30-a`. Keep `fx30-b` and `fx30-c` disabled until those cameras exist.

## One-Window Studio Startup

For normal use, open:

```text
/Users/jim/Developer/build/start_fx30_studio.command
```

The launcher checks each enabled camera first. If a camera is already trusted, it starts the bridge in the same window. If Sony still requires first-time approval, the launcher tells you to put the FX30 on the `Pairing` screen and then retries from the same window.

The launcher also runs the configured `zowieResolutionGuard` after the camera
check and before opening OBS. For each enabled ZowieBox it logs in to the local
device API, reads the full encoder configuration, corrects the main stream to
3840x2160 when necessary, and reads it back for verification. If the device is
unreachable, rejects the setting, or still reports another resolution, OBS is
not opened. This makes a silent 1080p recording a startup failure instead of a
surprise after the session.

The live Camera A config uses the unchanged factory credential only because
`allowFactoryDefaultPassword` is explicitly enabled. After changing that
password, set `allowFactoryDefaultPassword` to `false` and store the new value
in the macOS Keychain service named by `passwordEnv` (currently
`FX30_ZOWIE_A_PASSWORD`). The launcher uses the same Keychain-loading pattern
as the email integration and does not store the password in `config.json`.

This removes the old two-script flow. The one thing the Mac cannot safely automate is pressing `OK` on the camera when Sony firmware requires first-time trust approval.

When the bridge starts, it opens a warm SDK connection to each enabled camera. This avoids the 15-17 second Sony connection setup on every Companion record button press. If Companion still feels slow after an update, quit the studio command window and reopen `start_fx30_studio.command` so the warm connection is recreated.

After `REC STOP`, the bridge releases the warm Remote connection by default so `COPY FILES` can switch into Sony Contents Transfer mode faster. If another `REC START` is pressed before copying, the bridge will reconnect automatically.

The V4 ARM route also restores a released warm connection through guarded
`POST /record/warm`. The bridge refuses that reconnect while an FX30 recording,
record action, or ingest job is active. This lets the normal STOP-to-copy state
return to record-ready without launching a second bridge on port 3030.

The bridge also serves a live status dashboard:

```text
http://192.168.0.100:3030/
```

Open it on the studio Mac, or from another device on the same closed studio network. It refreshes automatically and shows recording state, warm-link status, last action timing, ingest state, recent files, and simple start/stop/copy controls.

`start_fx30_studio.command` opens the dashboard automatically on the studio Mac. To turn that off, set `openDashboard` to `false` in the config.

When `COPY FILES` runs, the dashboard shows the camera-reported proxy file size before copying, then polls the destination file as it grows to estimate percent complete and time remaining. When copying finishes, the bridge copies the proxy into LucidLink and sends the existing macOS/email notifications.

## Companion record-light display

`GET /record/status` now includes:

- `state.recordingElapsed` and `state.recordingElapsedSeconds` — elapsed time since the bridge confirmed record start.
- `storage.freeGB` and `storage.freeGBText` — free space on the configured local recording volume, using decimal GB.
- `storage.freeGiB` — the same free space using binary GiB.

The storage figure is the Mac/SSD recording destination (`ingestFolder`), not the camera card. This keeps the Companion status check fast and avoids repeatedly opening a Sony SDK connection just to read media capacity.

For a Stream Deck button, use `fx30_bridge/companion_record_status.py` in a Companion shell action. The `display` field prints three lines: `REC` or `READY`, the elapsed `HH:MM:SS`, and free space such as `842.6 GB free`. Run that action from a Companion **Time interval** trigger every second and store stdout in a custom variable such as `fx30_a_record_display`. Use the `recording` field in a second action if the button needs a red/green feedback variable.

For a camera-card capacity button, first press **Refresh card status** in the dashboard while the camera is idle. Then use `--field card-display`; it shows the Sony-reported remaining recording time for Slots 1 and 2. The Camera Remote SDK does not expose a dependable free-GB value. The card status is intentionally held while recording or copying so that polling cannot interrupt the camera connection.

The dashboard's **FORMAT SLOT** control is deliberately a recovery-only action: it is blocked during recording/copying, requires every tracked proxy backup to be size-verified both on the SSD and in LucidLink, requires Sony to report full format as available, and requires two exact typed acknowledgements. It does not prove that the high-resolution originals were manually copied; that remains a deliberate human confirmation immediately before formatting.

Stable files in `/Volumes/Extreme SSD/TEST-RECS/OBS-MASTERS` are also copied into the LucidLink `OBS-MASTERS` folder during `COPY FILES`. Existing same-size files are skipped safely.

### Companion Ready button

Add an HTTP request action to the Companion Ready button:

```text
POST http://192.168.0.100:3030/studio/ready
```

This opens the LucidLink app and checks whether the configured filespace is mounted. Companion will receive HTTP 200 when ready and HTTP 503 when LucidLink still needs the filespace connected. Press Ready again after connecting it. The normal copy action remains:

```text
POST http://192.168.0.100:3030/record/ingest
```

The dashboard also shows email delivery status after ingest: `SENT`, `FAILED`, `SKIPPED`, or `WAITING`. Email failure is shown separately and does not mark the file copy itself as failed.

Existing ingest completion notifications remain inline in the bridge. Proven
FX30 start/stop command failures use the shared one-shot helper at
`/Users/jim/Developer/build/studio_notification.command`, including its 30-second
event-key suppression. The latest result remains available through
`state.last_mac_notification`. A successful submission does not bypass macOS
Focus or prove that the operator saw the banner.

## Camera Pairing

Pair each FX30 with the Mac once before using Companion.

On the FX30:

```text
Network -> Transfer/Remote -> FTP Transfer Func. -> Off
Network -> Transfer/Remote -> PC Remote Function -> On
Network -> Transfer/Remote -> PC Remote Connection Method -> Wi-Fi Access Point
```

If the camera shows:

```text
Pairing
Perform pairing on the connected device.
```

leave that screen open, then run:

```text
/Users/jim/Developer/build/start_fx30_studio.command
```

The launcher will retry the SDK trust check. If the camera changes to a confirmation screen, press `OK` on the camera. If it asks for a password, Access Authentication is still enabled.

After pairing completes, turn the camera off, wait about 20 seconds, then turn it back on. Sony notes that this restart saves the pairing information.

Close any Sony remote app before testing Companion. If pairing is not complete, `sony-rec` may fail with `Connection canceled` or time out while the camera waits for approval.

If the launcher reports `SONY_REC_PASSWORD is required`, Access Authentication is enabled on the camera. For a no-password closed setup, turn Access Authentication off in the camera network settings if your firmware allows it. If the camera does not allow that, Sony requires the password and the bridge cannot bypass it.

For a closed studio test network, the camera may also offer `Connect without Pairing`. That can bypass first-time pairing, but it is less secure.

If the Sony sample shows:

```text
[CAT: Connect] [DETAILS: Connection rejected and failed]
```

the camera is visible on the network but is refusing the SDK control connection. Put the FX30 back on the `Pairing` screen and run the pairing helper again. If it still rejects, temporarily enable `Connect without Pairing` on the FX30 to prove the control path.

If the bridge terminal shows:

```text
[CAT: Connect] [DETAILS: A connection request failed through the Ethernet]
Timed out waiting for Sony SDK connection to become ready
```

the Mac reached the camera, but the camera did not complete the SDK connection. First close Imaging Edge Desktop and the interactive `RemoteCli`/pairing terminal, because only one Sony controller can own the camera at a time. Then wait 20-30 seconds after stopping recording before pressing `Copy Files`. The `sony-rec` helper retries the connection a few times, but the FX30 can still reject transfer mode if another app is connected or the camera is still finishing the movie file.

On this FX30, Sony's own sample refused `Remote Transfer Mode` over Wi-Fi with that Ethernet error. The bridge therefore uses `Contents Transfer Mode` for `/record/ingest`. If ingest still fails, manually test the same path in Sony's sample:

```text
1   ILME-FX30
2   Connect (Contents Transfer Mode)
1   Get contents list
```

If this manual contents-transfer test cannot list or download files either, the SDK wireless copy path is blocked in the current camera/network settings. Do not switch to camera-native FTP for unattended operation unless you are deliberately testing a non-PC-Remote fallback, because FTP can break the no-touch PC Remote workflow.

## Companion Errors

If Companion reports:

```text
Response code 500
Call timed out
```

the HTTP button reached the bridge, but the Sony camera command did not complete. The most common causes are:

```text
Camera is still on the Pairing screen
Camera has not been paired with this Mac yet
Imaging Edge Desktop is still open and using the camera
PC Remote Connection Method is not Wi-Fi Access Point
Mac and camera are not on the same Wi-Fi
```

If Companion reports:

```text
Unable to set the value of variable $(custom:undefined)
```

that is a Companion button/variable action issue, not the FX30 bridge itself. Remove any action that tries to write to `$(custom:undefined)`.

## Setup

```text
cp config.example.json config.json
python3 bridge.py --config config.example.json
```

Companion buttons:

```text
POST http://192.168.0.100:3030/record/start
POST http://192.168.0.100:3030/record/stop
POST http://192.168.0.100:3030/record/ingest
GET  http://192.168.0.100:3030/record/status
```

`/record/start`, `/record/stop`, and `/record/ingest` start background jobs and return immediately so Companion does not time out while the Sony SDK connects, records, stops, or copies files. Watch the bridge terminal or call `/record/status` to see the final result.

The status endpoint also checks each configured ingest folder and lists files whose size/modified timestamp have been stable long enough to count as arrived.

When an ingest finishes, the bridge also sends a macOS notification such as `FX30 ingest: Downloaded C1924S03.MP4`. You can turn that off by setting `macNotifications` to `false` in the config.

## External Drive and LucidLink Delivery

The studio config uses the external SSD as the camera ingest landing folder:

```text
/Volumes/Extreme SSD/TEST-RECS/FX30_A
/Volumes/Extreme SSD/TEST-RECS/FX30_B
/Volumes/Extreme SSD/TEST-RECS/FX30_C
```

After the Sony SDK copy finishes, the bridge copies the completed file into LucidLink:

```text
/Volumes/specialized2026/studio/Studio Recordings/FX30 Camera Footage/FX30_A
/Volumes/specialized2026/studio/Studio Recordings/FX30 Camera Footage/FX30_B
/Volumes/specialized2026/studio/Studio Recordings/FX30 Camera Footage/FX30_C
```

This is controlled by the `lucidLink` block in `config.json`. Keep `lucidLink.enabled` set to `true` only when the LucidLink filespace is mounted.

Email notification support is also configured in `config.json`, but it is disabled until sender details are added. The current config is set up for Resend. To enable it, set `email.enabled` to `true`, fill in `from` and `to`, then provide the API key through an environment variable:

```text
FX30_RESEND_API_KEY
```

SMTP is still supported if `email.provider` is changed to `smtp`. For SMTP, fill in `smtpHost`, `from`, and `to`, then provide the login through environment variables:

```text
FX30_SMTP_USERNAME
FX30_SMTP_PASSWORD
```

For a Stream Deck/Companion done indicator, create a `STATUS` button with:

```text
GET http://192.168.0.100:3030/record/status
```

This status check is local to the bridge and should return quickly.

The JSON response includes:

```text
state.last_ingest_ok
state.last_ingest_file
state.last_ingest_message
state.record_action_running
state.record_action
state.last_results
```

For the first proof, the Mac notification is the easiest done signal. A proper green/red Companion feedback can be added once the HTTP module is saving response fields cleanly.

Default ingest folders:

```text
/Users/jim/Movies/FX30_A
/Users/jim/Movies/FX30_B
/Users/jim/Movies/FX30_C
```
