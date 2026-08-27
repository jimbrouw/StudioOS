# Just In Mac Lite NDI V4 — local endurance mode

V4 is the local studio recording service. The legacy V3 route and Companion
page 3 remain the rollback.

## Headless NDI backup recovery V1

The recovery layer is implemented beside V4, but is **disabled in the live
`config.json`** and remains local-test only. It never changes an active
recording, sends a camera command, changes a ZowieBox, deletes source media, or
accepts a remote-control command.

When enabled for a controlled fixture rehearsal it:

- creates one atomic session manifest and append-only, idempotent events under
  `/Volumes/Extreme SSD/STUDIO-RECOVERY/SESSIONS/<session-id>`;
- uses the approved mapping `NDI 1 / Channel1 -> Camera 1 / fx30-a`;
- leaves configured Camera 2 and Camera 3 entries disabled, rather than
  removing them or adding invalid JSON comments;
- observes Just In status, recording paths and local file growth without
  controlling the recorder;
- waits for a verified terminal STOP before evaluating recovery;
- accepts exactly one local Sony fixture candidate for the failed mapping;
- copies it through versioned staging and verified delivery without modifying
  the candidate source; and
- escalates, without delivery, when the recovered Sony duration differs from
  the failed NDI duration by more than 30 seconds or evidence is ambiguous.

The recovery code currently uses an explicit local fixture candidate directory
only. It does not yet catalogue or download a real FX30 card automatically.
That hardware path must remain disabled until a controlled Sony catalog/selected
ingest proof and the unresolved ZowieBox recording fault are both complete.

`events.jsonl` is the sole remote-notifier contract. A consumer may read the
authoritative events, retain them for 30 days and notify Jim, but it cannot send
commands to V4. A missing heartbeat means **status unknown**, not recording
failure.

Page 4's **FX30 TEST 1 CAM** mode records all cameras currently enabled in
`fx30_bridge/config.json`. The present config enables only `fx30-a`, so ARM
requires that one warm Wi-Fi connection and RECORD/STOP include that camera
alongside the NDI, OBS, and RØDECaster actions.

## Storage behavior

```text
Just In masters: /Volumes/specialized2026/studio/Studio Recordings/Just-In Masters
Just In proxies: /Volumes/specialized2026/studio/Studio Recordings/Just-In Proxies
RØDECaster:     /Volumes/specialized2026/studio/Studio Recordings/RØDECaster Audio (manual only)
LucidLink:      the approved delivery location
```

- STOP waits for the three Just In files to finalise, copies verified masters
  to LucidLink, and creates playable 1080p H.264/AAC proxies there.
- The V4 post-record card shows the actual proxy elapsed time and, once a
  first file completes, an estimate of the remaining time for that take. It
  also keeps the final total and per-source-file timing for the last verified job.
- Masters are not duplicated or hashed again because they are already on the
  target SSD. Their post-record proof is stability/finalisation, not a second
  copy checksum.
- The three-hour pre-flight combines the measured master rate and proxy rate
  against the Extreme SSD's free space with the 15% margin.
- RØDECaster remaining time is never claimed as a live API reading. Before ARM
  or RECORD, read it from the supported device screen/app and confirm it in the
  dashboard. The gate is 207 minutes (3 hours plus the configured 15% margin).
  That reading carries through ordinary, fully verified takes in the current
  bridge session and production day; each verified stop reduces the projection.
- An explicit phrase-and-reason override lasts five minutes and one successful
  start. It is always amber in the dashboard and Companion, never green.
- The optional RØDECaster action copies to the SSD and LucidLink, verifies both
  copies, retains the microSD source, and does not run automatically after STOP.
- A green V4 result proves only the completed, verified destinations shown in
  that result.

## After STOP: what must stay open

`start_just_control_v4_local.command` starts **V4**: the local control service
and its post-record worker. The Terminal window running that command must stay
open until the dashboard says the post-record job is **complete** or **needs
attention**. Closing that Terminal stops the worker and interrupts any active
proxy encode; incomplete proxies are left as hidden `.partial.mp4` files and
will be discarded and re-encoded on the next V4 run.

Once STOP has been accepted for all three channels and the dashboard has moved
to post-record processing, OBS and Just In Mac Lite NDI may be closed. Proxy
creation reads the completed master files from the Extreme SSD and uses local
FFmpeg; it does not require OBS or the Just In app to remain open. Closing the
dashboard browser tab is also safe, provided the Terminal service itself is
still running.

Do not begin a new take until the active post-record job has reached a terminal
state. In particular, leave V4 running while it is showing **Creating proxy
files**, even if the first counter remains at `0 of 6`: files are encoded and
verified one at a time, so it does not increment until the first complete proxy
has finished.

## macOS failure banners

V4 uses `/Users/jim/Developer/build/studio_notification.command` for one-shot native
macOS notifications. Proven Just In start/stop failures and terminal post-record
failure, cancellation, or verification-attention states notify the operator.
Verified post-record success is optional and is disabled by default in
`postRecord.macNotifications.postRecordSuccess`.

The helper suppresses the same event key for 30 seconds and logs submission
receipts to `/private/tmp/studio_notifications.log`. A successful receipt means
macOS accepted the request; Focus and per-app notification settings can still
hide or delay it. These banners do not prove OBS or RØDECaster MIDI state.

The controlled visual proof is a separately approved, clearly labelled test of
the helper only. Unit tests use mocks and do not contact Companion or hardware.

## Start and API

```bash
/Users/jim/Developer/build/fx30_just_control_v4_local/start_just_control_v4_local.command
```

Dashboard: `http://127.0.0.1:3060/`

```text
GET  /v4/status
GET  /v4/storage
POST /v4/prepare
POST /v4/storage/refresh
POST /v4/capacity/confirm
POST /v4/capacity/override
POST /v4/capacity/recover
POST /v4/storage/rodecaster-archive
POST /v4/files/open
POST /v4/record/start
POST /v4/record/stop
POST /v4/record/recover-failed-stop
POST /v4/ingest
POST /v4/post-record/cancel
POST /v4/post-record/acknowledge
```

`/v4/post-record/acknowledge` is idle-only. It dismisses a terminal historical
attention receipt without retrying, moving, deleting, or rewriting any media.

`/v4/record/recover-failed-stop` is also idle-only and is intentionally not a
STOP retry. It is available only when V4 still retains a recording/session
flag, all requested Just In channels explicitly report stopped, and no
post-record work is active. It then reads OBS `GetRecordStatus`, reads the
FX30 bridge in camera mode, and requires an explicit operator confirmation
that the physical RØDECaster REC light and timer are off. It sends no recorder,
OBS, camera, Companion, MIDI, media, or post-record command. On success it
records the failed-STOP recovery receipt, clears only V4's stale state, and
retires the guarded lock and one-shot STOP marker; RØDECaster capacity remains
in attention until a new device reading is entered.

See [COMPANION_V4.md](COMPANION_V4.md) for page 4 wiring.

## Capacity guardian proof boundary

The V4 capacity guardian only records operator evidence; it does not scrape or
automate the RØDECaster UI, mount its media, delete media, or stop recording.
One device/app reading is a trusted baseline for the current bridge session and
production day. Each fully verified stop subtracts the completed take elapsed
time, rounded up to a full minute. It blocks and requests a fresh device/app
check after a bridge restart, day rollover, malformed evidence, an incomplete
or unknown take, or when the projected value falls below the required threshold.
The amber override remains visible and is consumed after one successful start
or five minutes; it never becomes green.
