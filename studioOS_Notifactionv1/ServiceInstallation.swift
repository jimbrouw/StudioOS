import Foundation
import ServiceManagement
import SwiftUI
import Combine
import AppKit

enum StudioOSServiceState: Equatable {
    case notRegistered, registered, awaitingApproval, unavailable

    var label: String {
        switch self {
        case .notRegistered: "Not registered"
        case .registered: "Registered"
        case .awaitingApproval: "Awaiting approval"
        case .unavailable: "Unavailable"
        }
    }
}

struct StudioOSServiceHealth: Equatable {
    let port: Int
    let reachable: Bool
    let detail: String
}

@MainActor
final class ServiceInstallationCoordinator: ObservableObject {
    static let fx30Plist = "com.buildstudio.studioos.fx30-bridge.plist"
    static let watchdogPlist = "com.buildstudio.studioos.test-watchdog.plist"
    static let v4Plist = "com.buildstudio.studioos.v4.plist"

    @Published private(set) var fx30State: StudioOSServiceState = .notRegistered
    @Published private(set) var watchdogState: StudioOSServiceState = .notRegistered
    @Published private(set) var v4State: StudioOSServiceState = .notRegistered
    @Published private(set) var fx30Health = StudioOSServiceHealth(port: 3030, reachable: false, detail: "Not checked")
    @Published private(set) var watchdogHealth = StudioOSServiceHealth(port: 3061, reachable: false, detail: "Not checked")
    @Published private(set) var v4Health = StudioOSServiceHealth(port: 3060, reachable: false, detail: "Not checked")
    @Published private(set) var actionMessage: String?

    private let fx30LegacyURL = URL(fileURLWithPath: "/Library/LaunchAgents/com.buildstudio.studioos.fx30-bridge.plist")
    private let watchdogLegacyURL = URL(fileURLWithPath: "/Library/LaunchAgents/com.buildstudio.studioos.test-watchdog.plist")
    private let v4LegacyURL = URL(fileURLWithPath: "/Library/LaunchAgents/com.buildstudio.studioos.v4.plist")

    func refresh() async {
        fx30State = state(forLegacyPlist: fx30LegacyURL)
        watchdogState = state(forLegacyPlist: watchdogLegacyURL)
        v4State = state(forLegacyPlist: v4LegacyURL)
        async let fx30Result = health(url: URL(string: "http://127.0.0.1:3030/health")!, port: 3030)
        async let watchdogResult = health(url: URL(string: "http://127.0.0.1:3061/watchdog/status")!, port: 3061)
        async let v4Result = health(url: URL(string: "http://127.0.0.1:3060/v4/status")!, port: 3060)
        fx30Health = await fx30Result
        watchdogHealth = await watchdogResult
        v4Health = await v4Result
    }

    func registerConfiguredServices(using folders: RecordingFolderAccessStore) async {
        do {
            try ServiceRuntimeConfiguration.write(from: folders)
            actionMessage = "Configuration written. The installer-owned background services will start automatically; this can take up to 30 seconds."
        } catch {
            actionMessage = "StudioOS could not write the service configuration: \(error.localizedDescription)"
        }
        await refresh()
    }

    func openLoginItems() {
        SMAppService.openSystemSettingsLoginItems()
    }

    func copyDiagnostics() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(diagnosticReport(), forType: .string)
        actionMessage = "StudioOS diagnostics copied to the clipboard."
    }

    private func state(forLegacyPlist url: URL) -> StudioOSServiceState {
        guard FileManager.default.fileExists(atPath: url.path) else { return .unavailable }
        switch SMAppService.statusForLegacyPlist(at: url) {
        case .enabled: return .registered
        case .requiresApproval: return .awaitingApproval
        case .notRegistered: return .notRegistered
        case .notFound: return .unavailable
        @unknown default: return .unavailable
        }
    }

    private func diagnosticReport() -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let configDirectory = home.appendingPathComponent("Library/Application Support/StudioOS/Config", isDirectory: true)
        let logDirectory = home.appendingPathComponent("Library/Logs/StudioOS", isDirectory: true)
        let fx30HealthLabel = fx30Health.reachable ? "reachable" : "not reachable"
        let watchdogHealthLabel = watchdogHealth.reachable ? "reachable" : "not reachable"
        let v4HealthLabel = v4Health.reachable ? "reachable" : "not reachable"
        let fx30Config = configDirectory.appendingPathComponent("fx30-bridge.json")
        let watchdogConfig = configDirectory.appendingPathComponent("test-watchdog.json")
        let v4Config = configDirectory.appendingPathComponent("v4.json")
        let fx30Log = logDirectory.appendingPathComponent("fx30-bridge.log")
        let watchdogLog = logDirectory.appendingPathComponent("test-watchdog.log")
        let v4Log = logDirectory.appendingPathComponent("v4.log")
        return [
            "StudioOS service diagnostics",
            "Generated: \(ISO8601DateFormatter().string(from: Date()))",
            "App: \(Bundle.main.bundleURL.path)",
            "FX30 LaunchAgent installed: \(FileManager.default.fileExists(atPath: fx30LegacyURL.path))",
            "FX30 service state: \(fx30State.label)",
            "FX30 health: \(fx30HealthLabel) - \(fx30Health.detail)",
            "Watchdog LaunchAgent installed: \(FileManager.default.fileExists(atPath: watchdogLegacyURL.path))",
            "Watchdog service state: \(watchdogState.label)",
            "Watchdog health: \(watchdogHealthLabel) - \(watchdogHealth.detail)",
            "V4 LaunchAgent installed: \(FileManager.default.fileExists(atPath: v4LegacyURL.path))",
            "V4 service state: \(v4State.label)",
            "V4 health: \(v4HealthLabel) - \(v4Health.detail)",
            "FX30 config exists: \(FileManager.default.fileExists(atPath: fx30Config.path))",
            "Watchdog config exists: \(FileManager.default.fileExists(atPath: watchdogConfig.path))",
            "V4 config exists: \(FileManager.default.fileExists(atPath: v4Config.path))",
            "FX30 log: \(fx30Log.path)",
            "Watchdog log: \(watchdogLog.path)",
            "V4 log: \(v4Log.path)"
        ].joined(separator: "\n")
    }

    private func health(url: URL, port: Int) async -> StudioOSServiceHealth {
        do {
            var request = URLRequest(url: url)
            request.timeoutInterval = 2
            let (_, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
                return StudioOSServiceHealth(port: port, reachable: false, detail: "HTTP response was not healthy")
            }
            return StudioOSServiceHealth(port: port, reachable: true, detail: "Read-only health response received")
        } catch {
            return StudioOSServiceHealth(port: port, reachable: false, detail: "No local response")
        }
    }
}

enum ServiceRuntimeConfiguration {
    private static let folderRoles: [RecordingFolderRole] = [.justInMasters, .obsMasters, .obsStems, .proxies, .rodecasterAudio]

    static func write(from folders: RecordingFolderAccessStore) throws {
        let selected = try Dictionary(uniqueKeysWithValues: folderRoles.map { role in
            guard folders.isReady(role), let path = folders.displayPath(for: role), !path.isEmpty else {
                throw CocoaError(.fileNoSuchFile, userInfo: [NSLocalizedDescriptionKey: "Choose and validate \(role.title) before registering services."])
            }
            return (role, path)
        })

        let support = try directory(.applicationSupportDirectory, appending: "StudioOS")
        let configDirectory = support.appendingPathComponent("Config", isDirectory: true)
        let stateDirectory = support.appendingPathComponent("State", isDirectory: true)
        let logDirectory = try directory(.libraryDirectory, appending: "Logs/StudioOS")
        try FileManager.default.createDirectory(at: configDirectory, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: stateDirectory, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: logDirectory, withIntermediateDirectories: true)

        let ffmpeg = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/Tools/ffmpeg").path
        let ffprobe = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/Tools/ffprobe").path
        let controls = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/Controls", isDirectory: true)
        let fx30: [String: Any] = [
            "schemaVersion": 1,
            "host": "127.0.0.1",
            "port": 3030,
            "openDashboard": false,
            "macNotifications": false,
            "obs": ["masterFolder": selected[.obsMasters]!, "audioStemsFolder": selected[.obsStems]!, "copyStableFiles": false, "copyAudioStems": false, "expectedCameraCount": 0],
            "cameras": [[String: Any]](),
            "zowieResolutionGuard": ["enabled": false],
            "nativeFtp": ["enabled": false],
            "studioOS": ["justInMasters": selected[.justInMasters]!, "proxies": selected[.proxies]!, "rodecasterAudio": selected[.rodecasterAudio]!]
        ]
        let watchdog: [String: Any] = [
            "schemaVersion": 1,
            "machine": ["identifier": Host.current().localizedName ?? "studio-mac"],
            "service": ["host": "127.0.0.1", "port": 3061],
            "justIn": ["host": "127.0.0.1", "port": 8080, "timeoutSeconds": 5, "channels": ["Channel1", "Channel2", "Channel3"]],
            "paths": ["justInMasters": selected[.justInMasters]!, "obsMasters": selected[.obsMasters]!, "obsStems": selected[.obsStems]!, "statusFile": stateDirectory.appendingPathComponent("watchdog-status.json").path, "sessionFile": stateDirectory.appendingPathComponent("watchdog-session.json").path, "eventLog": logDirectory.appendingPathComponent("watchdog-events.jsonl").path, "serviceLogDirectory": logDirectory.path],
            "tools": ["ffprobe": ffprobe],
            "verification": ["stableSeconds": 3, "settleTimeoutSeconds": 120, "pollSeconds": 1, "sessionStartToleranceSeconds": 2, "justInExtensions": [".mov", ".mp4", ".mxf"], "obsMasterExtensions": [".mp4", ".mov", ".mkv"], "obsStemExtensions": [".wav", ".aif", ".aiff", ".flac", ".m4a"], "minimumObsStemCount": 1]
        ]
        let v4: [String: Any] = [
            "schemaVersion": 1,
            "api": ["host": "127.0.0.1", "port": 8080, "timeoutSeconds": 5],
            "bridge": ["host": "127.0.0.1", "port": 3060, "stableSeconds": 3, "settleTimeoutSeconds": 120],
            "sourceRoot": selected[.justInMasters]!,
            "backupRoot": NSNull(),
            "deliveryRoot": selected[.justInMasters]!,
            "proxyWorkingRoot": selected[.proxies]!,
            "proxyDeliveryRoot": selected[.proxies]!,
            "delivery": [
                "provider": "Local StudioOS",
                "justInMasters": selected[.justInMasters]!,
                "justInProxies": selected[.proxies]!,
                "obsMasters": selected[.obsMasters]!,
                "obsAudioStems": selected[.obsStems]!,
                "rodecasterAudio": selected[.rodecasterAudio]!
            ],
            "extensions": [".mov", ".mp4", ".mxf", ".wav"],
            "proxyExtensions": [".mov", ".mp4", ".mxf"],
            "postRecord": [
                "enabled": true,
                "localOnly": true,
                "ffmpegPath": ffmpeg,
                "ffprobePath": ffprobe,
                "proxyHeight": 1080,
                "proxyVideoBitrate": "10M",
                "proxyAudioBitrate": "192k",
                "macNotifications": ["enabled": false, "postRecordSuccess": false]
            ],
            "storagePolicy": [
                "targetHours": 3,
                "safetyMarginPercent": 15,
                "extremeSsdBaselineGBPerHour": 607,
                "proxyBaselineGBPerHour": 14,
                "rodecasterBaselineGBPerHour": NSNull(),
                "rodecasterStaleHours": 24,
                "rodecasterConfirmationMinutes": 15,
                "rodecasterOverrideMinutes": 5,
                "rodecasterOverridePhrase": "OVERRIDE RØDECASTER CAPACITY",
                "includeRodecasterInPreflight": false,
                "includeRodecasterInCompletion": false,
                "stateFile": stateDirectory.appendingPathComponent("v4-storage-state.json").path,
                "modeFile": stateDirectory.appendingPathComponent("studio-record-mode").path,
                "fx30StatusUrl": "http://127.0.0.1:3030/record/status",
                "fx30RefreshUrl": "http://127.0.0.1:3030/record/card-status",
                "rodecasterCleanupCommand": [
                    controls.appendingPathComponent("run-rodecaster-archive").path,
                    selected[.rodecasterAudio]!,
                    stateDirectory.appendingPathComponent("v4-storage-state.json").path
                ]
            ],
            "requireAllChannels": true,
            "channels": [
                ["name": "Channel1", "enabled": true],
                ["name": "Channel2", "enabled": true],
                ["name": "Channel3", "enabled": true]
            ],
            "recovery": ["enabled": false, "localTestOnly": true, "mappings": [[String: Any]]()],
            "failedStopRecovery": [
                "recordingLockPath": "/private/tmp/strict_studio_recording.lock",
                "stopMarkerPath": "/private/tmp/strict_studio_stop_v4_local.sent",
                "obs": ["host": "127.0.0.1", "port": 4455, "timeoutSeconds": 3, "passwordEnvironment": "OBS_WEBSOCKET_PASSWORD"]
            ],
            "noCameraRecord": [
                "recordCommand": controls.appendingPathComponent("record_no_camera_v4_local.command").path,
                "stopCommand": controls.appendingPathComponent("strict_studio_stop_v4_local.command").path,
                "armReceiptDirectory": support.appendingPathComponent("Receipts/no-camera", isDirectory: true).path,
                "sessionPath": stateDirectory.appendingPathComponent("no-camera-recording.session").path
            ]
        ]
        try writeJSON(fx30, to: configDirectory.appendingPathComponent("fx30-bridge.json"))
        try writeJSON(watchdog, to: configDirectory.appendingPathComponent("test-watchdog.json"))
        try writeJSON(v4, to: configDirectory.appendingPathComponent("v4.json"))
    }

    private static func directory(_ search: FileManager.SearchPathDirectory, appending path: String) throws -> URL {
        guard let base = FileManager.default.urls(for: search, in: .userDomainMask).first else { throw CocoaError(.fileNoSuchFile) }
        return base.appendingPathComponent(path, isDirectory: true)
    }

    private static func writeJSON(_ value: [String: Any], to url: URL) throws {
        let data = try JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: url, options: .atomic)
    }
}

struct InstallationSetupView: View {
    @ObservedObject var folders: RecordingFolderAccessStore
    @ObservedObject var notifications: LocalNotificationManager
    @StateObject private var services = ServiceInstallationCoordinator()

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                Text("Set up StudioOS").font(.largeTitle.bold())
                Text("Set up the local services. This does not change the Studio page buttons.").foregroundStyle(.secondary)
                GroupBox("Before registering") {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Point StudioOS at your five recording folders.")
                        DisclosureGroup("Details") {
                            Text("StudioOS saves machine-specific, non-secret configuration. It does not create folders or confirm that recorder folders are correct. Select the actual JustIn, OBS-MASTERS, AUDIO-STEMS, Proxies and AUDIO/RODECASTER folders on this Mac.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        ForEach(RecordingFolderRole.allCases) { role in
                            HStack { VStack(alignment: .leading) { Text(role.title); Text(folders.checks[role]?.title ?? "Not chosen yet").font(.caption).foregroundStyle(.secondary) }; Spacer(); Button("Choose") { folders.chooseFolder(for: role) } }
                        }
                        HStack { Button("Check folders") { folders.checkAll() }; Button("Allow notifications") { notifications.requestPermission() }; Text("Notifications: \(notifications.statusLabel)").font(.caption).foregroundStyle(.secondary) }
                    }.padding(6)
                }
                GroupBox("Background services") {
                    VStack(alignment: .leading, spacing: 12) {
                        serviceRow("FX30 bridge", port: 3030, state: services.fx30State, health: services.fx30Health)
                        serviceRow("Production V4 coordinator", port: 3060, state: services.v4State, health: services.v4Health)
                        serviceRow("Test-only watchdog", port: 3061, state: services.watchdogState, health: services.watchdogHealth)
                        HStack { Button("Write configuration and start services") { Task { await services.registerConfiguredServices(using: folders) } }.buttonStyle(.borderedProminent); Button("Refresh status") { Task { await services.refresh() } }; Button("Login Items") { services.openLoginItems() }; Button("Copy diagnostics") { services.copyDiagnostics() } }
                        Text("A running service is not proof of a recording or delivered media.").font(.caption).foregroundStyle(.secondary)
                        DisclosureGroup("Details") {
                            Text("The installer owns these three LaunchAgents. If a row says Awaiting approval, enable the StudioOS background item under System Settings > General > Login Items & Extensions, then refresh. A reachable health endpoint means only that a process responded.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        if let message = services.actionMessage { Text(message).font(.callout) }
                    }.padding(6)
                }
                GroupBox("Control boundary") { Text("V4 on 3060 is installed as the local coordinator. Installation and health checks never send ARM, RECORD, STOP, camera, OBS, Companion or RØDECaster commands.").foregroundStyle(.secondary).padding(6) }
            }.padding(28)
        }.task { await services.refresh() }
    }

    private func serviceRow(_ name: String, port: Int, state: StudioOSServiceState, health: StudioOSServiceHealth) -> some View {
        let isRunning = state == .registered && health.reachable
        return HStack { VStack(alignment: .leading) { Text("\(name) · localhost:\(port)").font(.headline); Text(isRunning ? "Running" : "\(state.label) · \(health.reachable ? "Reachable" : "Not reachable")").font(.caption).foregroundStyle(.secondary) }; Spacer(); StatusPill(text: isRunning ? "RUNNING" : state.label.uppercased(), color: isRunning ? .green : (state == .awaitingApproval ? .orange : .secondary)) }
    }
}
