# Companion page 4 — Local V4 Endurance

Page 4 is intentionally separate from the legacy V3 route on page 3.

| Visible action | Behavior |
|---|---|
| FX30 TEST 1 CAM | Selects the endurance route: NDI recorders plus every camera enabled in `fx30_bridge/config.json` (currently `fx30-a`) |
| ARM | Checks enabled FX30 cameras, Just In, OBS, RØDECaster MIDI, and required storage |
| RECORD | Starts the selected strict or NDI-test recording chain |
| STOP | Stops recorders and queues verified Just In master/proxy delivery to LucidLink |
| RØDE ARCHIVE | Optional manual SSD + LucidLink archive; never deletes the card |
| CAPACITY STATUS | Opens the local dashboard and reports the canonical fail-closed capacity state |

Bridge base URL: `http://127.0.0.1:3060`

The Page 4 STOP action owns only the reviewed V4 stop and post-record workflow.
Do not use the legacy Page 3 coordinator for a V4 take.

In FX30 test mode, RECORD and STOP use the FX30 bridge actions as well as Just
In, OBS, and RØDECaster. The legacy `ndi-test` mode remains available through
the command line as an FX30-bypass rollback, but Page 4 no longer selects it.

The visible RECORD command owns the guarded `/v4/record/start` call and waits
for confirmed Just In/NDI start before it presses the hidden Companion chain.
That hidden chain contains only FX30 start, OBS start, and RØDECaster MIDI.
This ordering is intentional: Companion runs button actions independently and
would otherwise continue to OBS/MIDI after a capacity 409. The visible STOP
command likewise owns `/v4/record/stop`; the hidden stop chain contains only
RØDECaster MIDI, OBS stop, and FX30 stop.

STOP requires the guarded recording lock before it sends that hidden chain.
This matters because the RØDECaster command is a toggle: after a blocked start,
STOP must not turn an idle green RØDECaster into recording.

The first STOP also creates an atomic sent marker. A repeated STOP is blocked
instead of toggling the RØDECaster again. A newly confirmed guarded start clears
that marker.

Companion's internal shell action is limited to 20 seconds. The visible RECORD
button therefore calls `strict_studio_record_v4_local_launch.command`, which
reserves a single run and detaches the guarded coordinator. This prevents a
long guarded preflight from being killed before the other recorders are
started. Repeated presses return `RECORD BUSY` while that run is active.

ARM reads the FX30 bridge status but does not open or refresh a Sony camera
control connection. The camera connection and REC command are deferred until
after V4 has accepted and started the guarded recording.

ARM and RECORD do not force the optional Sony card-capacity refresh. The Sony
SDK can take up to 40 seconds and often returns no trustworthy remaining-time
value, which is warning-only. RECORD instead performs fast live checks for the
FX30 bridge, Just In V4 bridge, OBS, recording SSD, and RØDECaster device. The
V4 start endpoint remains the authoritative gate for live disk space, current
RØDECaster capacity, post-record state, and all requested Just In channels.

ARM does enforce the separate Zowie main-stream guard. Every enabled Zowie is
set to 3840x2160 and read back before ARM can succeed. An unreachable device,
missing credential, rejected write, or non-4K readback blocks RECORD rather
than allowing an unnoticed HD production take.

The enabled `RCP – RECORD` MIDI trigger also calls the same detached launcher.
It contains no direct legacy V2, OBS, or RØDECaster actions, so the Page 4
button and the RØDECaster smart pad share one guarded start owner.

The FX30 bridge keeps its Sony Remote-mode connection warm after a successful
STOP. Camera copy and recovery jobs still release that connection explicitly
when they need Contents Transfer mode. This avoids a cold 15-second reconnect
on the next RECORD without enabling automatic ingest.

`CAPACITY CONFIRMED` is green. `CAPACITY OVERRIDE` is amber. `CAPACITY BELOW`,
`CAPACITY STALE`, `CAPACITY UNAVAILABLE`, and `CAPACITY OFFLINE` are red.
