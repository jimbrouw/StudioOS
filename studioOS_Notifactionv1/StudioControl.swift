import Combine
import Foundation
import SwiftUI

/// The only Companion locations StudioOS is permitted to press. Hidden
/// sequence buttons, archive, camera and audio endpoints are deliberately
/// absent from this type. `noCameraArm` is an ARM-only test location.
enum VisiblePage4Control: String, CaseIterable {
    case arm = "4/0/2"
    case record = "4/1/0"
    case stop = "4/1/1"
    case noCameraArm = "4/2/2"

    var title: String {
        switch self {
        case .arm: "ARM"
        case .record: "RECORD"
        case .stop: "STOP"
        case .noCameraArm: "ARM: NO CAMERA"
        }
    }
}

enum StudioControlEngineState: Equatable {
    case checking
    case available
    case unavailable(String)

    var title: String {
        switch self {
        case .checking: "CHECKING CONTROL ENGINE"
        case .available: "CONTROL ENGINE AVAILABLE"
        case .unavailable: "CONTROL ENGINE OFFLINE"
        }
    }

    var tint: Color {
        switch self {
        case .available: .green
        case .checking: .orange
        case .unavailable: .red
        }
    }

    var detail: String {
        switch self {
        case .checking:
            "StudioOS is checking the local Companion service. No control has been sent."
        case .available:
            "Companion answered a read-only local availability check."
        case .unavailable(let reason):
            "Companion did not answer its local check: " + reason + ". RECORD and STOP are disabled."
        }
    }
}

struct StudioControlDispatch: Equatable {
    let control: VisiblePage4Control
    let sentAt: Date
    let releaseConfirmed: Bool
    let detail: String
}

enum LocalServiceState: Equatable {
    case checking
    case available
    case unavailable

    var label: String {
        switch self {
        case .checking: "CHECKING"
        case .available: "AVAILABLE"
        case .unavailable: "OFFLINE"
        }
    }

    var tint: Color {
        switch self {
        case .checking: .orange
        case .available: .green
        case .unavailable: .red
        }
    }
}

/// This is operator-entered evidence from the RØDECaster screen or its
/// supported app. StudioOS never reads, mounts, records to, or otherwise
/// controls the RØDECaster while showing or confirming this status.
struct RodecasterCapacity: Decodable, Equatable {
    let state: String
    let status: String
    let gateOpen: Bool
    let message: String
    let requiredMinutes: Double
    let remainingMinutes: Double?
    let source: String?
    let confirmedAt: String?

    static let unavailable = RodecasterCapacity(
        state: "unavailable",
        status: "unavailable",
        gateOpen: false,
        message: "RØDECaster capacity has not been checked yet.",
        requiredMinutes: 0,
        remainingMinutes: nil,
        source: nil,
        confirmedAt: nil
    )

    var title: String {
        switch state {
        case "confirmed": return "RØDE CONFIRMED"
        case "override": return "AMBER OVERRIDE"
        default: return state.replacingOccurrences(of: "-", with: " ").uppercased()
        }
    }

    var tint: Color {
        switch state {
        case "confirmed": return .green
        case "override", "below-threshold", "attention": return .orange
        default: return .red
        }
    }

    var projectedTime: String {
        guard let remainingMinutes else { return "No projected recording time" }
        return "\(Self.hoursText(fromMinutes: remainingMinutes)) projected remaining"
    }

    var requiredTime: String {
        Self.hoursText(fromMinutes: requiredMinutes) + " required"
    }

    var displayMessage: String {
        guard let remainingMinutes else { return message }
        if gateOpen {
            return "Projected \(Self.hoursText(fromMinutes: remainingMinutes)) remaining from operator baseline."
        }
        return "Projected \(Self.hoursText(fromMinutes: remainingMinutes)) remaining; \(Self.hoursText(fromMinutes: requiredMinutes)) required."
    }

    private static func hoursText(fromMinutes minutes: Double) -> String {
        let hours = minutes / 60
        let value = hours.rounded() == hours ? String(format: "%.0f", hours) : String(format: "%.1f", hours)
        return value + " hours"
    }
}

private struct V4StatusResponse: Decodable {
    let recording: Bool
    let storageReport: StorageReport
    let noCameraRecord: NoCameraRecordStatus?
}

struct NoCameraRecordStatus: Decodable, Equatable {
    let state: String
    let action: String?
    let detail: String?
}

private struct V4RequestError: Decodable {
    let error: String
}

private struct FX30StatusResponse: Decodable {
    struct RecordState: Decodable {
        let recordActionRunning: Bool

        enum CodingKeys: String, CodingKey {
            case recordActionRunning = "record_action_running"
        }
    }

    struct CameraStatus: Decodable {
        let enabled: Bool
        let warmConnection: Bool
    }

    let state: RecordState
    let cameraStatus: [CameraStatus]
}

private struct StudioControlRequestError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

private struct StorageReport: Decodable {
    let surfaces: [StorageSurface]
}

private struct StorageSurface: Decodable {
    let key: String
    let state: String?
    let status: String?
    let gateOpen: Bool?
    let message: String?
    let requiredMinutes: Double?
    let remainingMinutes: Double?
    let source: String?
    let confirmedAt: String?
}

@MainActor
final class StudioControlMonitor: ObservableObject {
    @Published private(set) var engineState: StudioControlEngineState = .checking
    @Published private(set) var lastContact: Date?
    @Published private(set) var dispatch: StudioControlDispatch?
    @Published private(set) var dispatchError: String?
    @Published private(set) var actionInProgress = false
    @Published private(set) var activeControl: VisiblePage4Control?
    @Published private(set) var fx30BridgeState: LocalServiceState = .checking
    @Published private(set) var fx30State: LocalServiceState = .checking
    @Published private(set) var obsState: LocalServiceState = .checking
    @Published private(set) var noCameraTestUntil: Date?
    @Published private(set) var rodecasterCapacity = RodecasterCapacity.unavailable
    @Published private(set) var v4Recording = false
    @Published private(set) var noCameraRecord = NoCameraRecordStatus(state: "idle", action: nil, detail: nil)
    @Published private(set) var noCameraRecordInProgress = false
    @Published private(set) var noCameraRecordMessage: String?
    @Published private(set) var noCameraRecordError: String?
    @Published private(set) var capacityConfirmationInProgress = false
    @Published private(set) var capacityConfirmationError: String?

    private let companionBaseURL: URL
    private var pollingTask: Task<Void, Never>?

    init(companionBaseURL: URL = URL(string: "http://127.0.0.1:8000")!) {
        self.companionBaseURL = companionBaseURL
    }

    var canDispatch: Bool {
        if case .available = engineState { return !actionInProgress }
        return false
    }

    func canPress(_ control: VisiblePage4Control, watchdog: WatchdogStatus) -> Bool {
        guard canDispatch else { return false }
        // STOP stays available whenever Companion is healthy. A fault in a
        // source must not remove the shared visible STOP path mid-take.
        guard control != .stop else { return true }
        // ARM owns service launch. It must remain available when OBS, V4, the
        // FX30 bridge, or a camera is offline; the guarded ARM coordinator
        // opens what it owns and then reports the real readiness failure.
        if control == .arm || control == .noCameraArm {
            return !v4Recording
        }
        // RECORD stays fail-closed on every live readiness gate.
        guard rodecasterCapacity.gateOpen else { return false }
        return fx30State == .available && obsState == .available && !watchdog.isWarning
    }

    func start() {
        guard pollingTask == nil else { return }
        pollingTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(3))
            }
        }
    }

    func stopPolling() {
        pollingTask?.cancel()
        pollingTask = nil
    }

    /// This request is read-only. Companion does not implement GET on button
    /// locations, so the local root is the availability probe. It never
    /// presses a location.
    func refresh() async {
        let checkURL = companionBaseURL
        do {
            var request = URLRequest(url: checkURL)
            request.httpMethod = "GET"
            request.cachePolicy = .reloadIgnoringLocalCacheData
            request.timeoutInterval = 2
            let (_, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
                throw URLError(.badServerResponse)
            }
            engineState = .available
            lastContact = Date()
        } catch {
            engineState = .unavailable(error.localizedDescription)
        }
        await refreshV4()
        await refreshFX30()
        await refreshOBS()
    }

    /// Sends a press to one of the three visible Page 4 locations. This must
    /// never be reused for hidden Companion locations or direct device APIs.
    func press(_ control: VisiblePage4Control, watchdog: WatchdogStatus) async {
        guard canPress(control, watchdog: watchdog) else {
            if control == .stop {
                dispatchError = "Companion is unavailable, so StudioOS did not send STOP. Use the physical/shared Page 4 control if available."
            } else if (control == .arm || control == .noCameraArm) && v4Recording {
                dispatchError = control.title + " is disabled while V4 confirms a recording is active."
            } else if !rodecasterCapacity.gateOpen {
                dispatchError = "RØDECaster capacity is blocking " + control.title + ": " + rodecasterCapacity.message
            } else if control == .record && fx30State != .available {
                dispatchError = "RECORD is disabled because no physical FX30 camera has a verified Sony control connection. Run ARM after connecting the camera."
            } else {
                dispatchError = control.title + " is disabled until Companion, V4, FX30 and OBS are all available."
            }
            return
        }

        actionInProgress = true
        activeControl = control
        dispatchError = nil
        defer {
            actionInProgress = false
            activeControl = nil
        }

        let locationURL = companionBaseURL
            .appendingPathComponent("api")
            .appendingPathComponent("location")
            .appendingPathComponent(control.rawValue)

        do {
            try await send(method: "POST", to: locationURL.appendingPathComponent("down"), timeout: 5)
        } catch {
            dispatchError = control.title + " was not sent to Companion: " + error.localizedDescription
            await refresh()
            return
        }

        try? await Task.sleep(for: .milliseconds(750))

        do {
            try await send(method: "POST", to: locationURL.appendingPathComponent("up"), timeout: 5)
            dispatch = StudioControlDispatch(
                control: control,
                sentAt: Date(),
                releaseConfirmed: true,
                detail: control == .noCameraArm
                    ? "ARM: NO CAMERA was accepted by Companion. It is an ARM-only test and does not start recording or enable no-camera RECORD. Check its separate receipt for the real readiness result."
                    : control.title + " press and release were accepted by Companion. This is control evidence, not recording or media proof."
            )
            if control == .noCameraArm {
                noCameraTestUntil = Date().addingTimeInterval(15 * 60)
            }
        } catch {
            dispatch = StudioControlDispatch(
                control: control,
                sentAt: Date(),
                releaseConfirmed: false,
                detail: control.title + " press was accepted, but Companion did not confirm release. Check the normal Page 4 state and the recorders."
            )
            dispatchError = dispatch?.detail
        }
        await refresh()
    }

    func clearNoCameraTestSession() {
        noCameraTestUntil = nil
    }

    func clearNoCameraMessages() {
        noCameraRecordMessage = nil
        noCameraRecordError = nil
    }

    var canStartNoCameraRecord: Bool {
        guard !actionInProgress, !noCameraRecordInProgress, !v4Recording else { return false }
        guard noCameraTestUntil.map({ $0 > Date() }) == true else { return false }
        guard rodecasterCapacity.gateOpen, obsState == .available, fx30State == .unavailable else { return false }
        if case .available = engineState {
            return noCameraRecord.state != "starting"
        }
        return false
    }

    var canStopNoCameraRecord: Bool {
        guard !noCameraRecordInProgress else { return false }
        return v4Recording && noCameraRecord.state != "stopping"
    }

    /// V4 starts the reviewed local coordinator. StudioOS never calls its
    /// hidden Companion route or recorder controls itself.
    func startNoCameraRecord() async {
        guard canStartNoCameraRecord else {
            noCameraRecordError = "RECORD: NO CAMERA needs a fresh successful ARM: NO CAMERA check, V4/OBS/Companion availability, a confirmed RØDECaster time, and no FX30 bridge available."
            return
        }
        await requestNoCameraCoordinator(path: "/v4/record/no-camera/start", success: "No-camera RECORD guard accepted the request. Wait for V4 to report RECORDING before treating it as started.")
    }

    func stopNoCameraRecord() async {
        guard canStopNoCameraRecord else {
            noCameraRecordError = "STOP: NO CAMERA is only available while V4 confirms the no-camera test is recording."
            return
        }
        await requestNoCameraCoordinator(path: "/v4/record/no-camera/stop", success: "No-camera STOP guard accepted the request. Press it once, then wait for V4 to confirm stopped and finalising.")
    }

    private func requestNoCameraCoordinator(path: String, success: String) async {
        noCameraRecordInProgress = true
        noCameraRecordError = nil
        noCameraRecordMessage = nil
        defer { noCameraRecordInProgress = false }
        do {
            let url = URL(string: "http://127.0.0.1:3060" + path)!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.timeoutInterval = 5
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
            guard (200...299).contains(http.statusCode) else {
                let message = (try? JSONDecoder().decode(V4RequestError.self, from: data))?.error ?? "V4 did not accept this guarded request."
                throw StudioControlRequestError(message)
            }
            noCameraRecordMessage = success
        } catch {
            noCameraRecordError = "No-camera control was not sent: " + error.localizedDescription
        }
        await refresh()
    }

    /// Saves an operator reading from the RØDECaster screen/app in the V4
    /// capacity guardian. It does not issue a RØDECaster, recorder, OBS or
    /// camera command. The operator must press this control deliberately.
    func confirmRodecasterCapacity(remainingHours: String, source: String) async {
        guard !v4Recording else {
            capacityConfirmationError = "Capacity cannot be changed during a recording. Stop and verify the take first."
            return
        }
        guard let hours = Double(remainingHours.trimmingCharacters(in: .whitespacesAndNewlines)), hours >= 0 else {
            capacityConfirmationError = "Enter the remaining recording time as zero or more hours."
            return
        }
        guard !source.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            capacityConfirmationError = "Say where you read the time, for example RØDECaster device screen."
            return
        }

        capacityConfirmationInProgress = true
        capacityConfirmationError = nil
        defer { capacityConfirmationInProgress = false }
        do {
            let url = URL(string: "http://127.0.0.1:3060/v4/capacity/confirm")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.timeoutInterval = 5
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: [
                "remainingMinutes": hours * 60,
                "source": source.trimmingCharacters(in: .whitespacesAndNewlines)
            ])
            let (_, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
                throw URLError(.badServerResponse)
            }
            await refreshV4()
        } catch {
            capacityConfirmationError = "StudioOS could not save the RØDECaster capacity check: " + error.localizedDescription
        }
    }

    private func send(method: String, to url: URL, timeout: TimeInterval) async throws {
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = timeout
        let (_, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
    }

    /// Reads the V4 storage report only. The report includes the same
    /// RØDECaster capacity gate used by guarded ARM/RECORD.
    private func refreshV4() async {
        do {
            var request = URLRequest(url: URL(string: "http://127.0.0.1:3060/v4/status")!)
            request.httpMethod = "GET"
            request.timeoutInterval = 2
            request.cachePolicy = .reloadIgnoringLocalCacheData
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
                throw URLError(.badServerResponse)
            }
            let status = try JSONDecoder().decode(V4StatusResponse.self, from: data)
            v4Recording = status.recording
            noCameraRecord = status.noCameraRecord ?? NoCameraRecordStatus(state: "unavailable", action: nil, detail: "This V4 service has not yet been updated for guarded no-camera RECORD.")
            guard let surface = status.storageReport.surfaces.first(where: { $0.key == "rodecaster-capacity" }) else {
                rodecasterCapacity = .unavailable
                return
            }
            rodecasterCapacity = RodecasterCapacity(
                state: surface.state ?? "unavailable",
                status: surface.status ?? "unavailable",
                gateOpen: surface.gateOpen ?? false,
                message: surface.message ?? "RØDECaster capacity status is unavailable.",
                requiredMinutes: surface.requiredMinutes ?? 0,
                remainingMinutes: surface.remainingMinutes,
                source: surface.source,
                confirmedAt: surface.confirmedAt
            )
        } catch {
            v4Recording = false
            noCameraRecord = NoCameraRecordStatus(state: "unavailable", action: nil, detail: "V4 status is unavailable.")
            rodecasterCapacity = .unavailable
        }
    }

    private func refreshFX30() async {
        do {
            var request = URLRequest(url: URL(string: "http://127.0.0.1:3030/record/status")!)
            request.httpMethod = "GET"
            request.timeoutInterval = 2
            request.cachePolicy = .reloadIgnoringLocalCacheData
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
                throw URLError(.badServerResponse)
            }
            let status = try JSONDecoder().decode(FX30StatusResponse.self, from: data)
            fx30BridgeState = .available
            let cameras = status.cameraStatus.filter(\.enabled)
            fx30State = !status.state.recordActionRunning && !cameras.isEmpty && cameras.allSatisfy(\.warmConnection)
                ? .available
                : .unavailable
        } catch {
            fx30BridgeState = .unavailable
            fx30State = .unavailable
        }
    }

    /// OBS WebSocket sends its Hello first, so receiving that first message is
    /// a read-only service check. StudioOS does not authenticate, configure or
    /// command OBS from this probe.
    private func refreshOBS() async {
        guard let url = URL(string: "ws://127.0.0.1:4455") else {
            obsState = .unavailable
            return
        }
        let task = URLSession.shared.webSocketTask(with: url)
        task.resume()
        do {
            _ = try await withThrowingTaskGroup(of: URLSessionWebSocketTask.Message.self) { group in
                group.addTask { try await task.receive() }
                group.addTask {
                    try await Task.sleep(for: .seconds(2))
                    throw URLError(.timedOut)
                }
                guard let result = try await group.next() else { throw URLError(.unknown) }
                group.cancelAll()
                return result
            }
            obsState = .available
        } catch {
            obsState = .unavailable
        }
        task.cancel(with: .normalClosure, reason: nil)
    }
}

struct StudioControlView: View {
    @ObservedObject var monitor: StudioControlMonitor
    @ObservedObject var watchdog: WatchdogStatusMonitor
    @ObservedObject var folders: RecordingFolderAccessStore
    @ObservedObject var evidence: RecordingEvidenceStore
    let showInstallation: () -> Void
    // Ten hours is the normal starting value, not automatic capacity proof.
    // The operator must still read the device/app and deliberately confirm it.
    @State private var rodecasterHours = "10"
    @State private var rodecasterSource = "RØDECaster device screen"
    @State private var physicalRodecasterConfirmedOff = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Studio Control").font(.largeTitle.bold())
                    Text("A guarded front panel for visible Companion Page 4 controls. It never calls hidden buttons, cameras, OBS, RØDECaster or archive routes directly.")
                        .foregroundStyle(.secondary)
                }

                if folders.folders.isEmpty {
                    GroupBox {
                        HStack(alignment: .center, spacing: 12) {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .foregroundStyle(.orange)
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Finish setup first").font(.headline)
                                Text("Choose the recording folders before the first take.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Button("Open setup", action: showInstallation)
                        }
                        .padding(4)
                    }
                }

                GroupBox("Control engine") {
                    HStack(alignment: .top, spacing: 12) {
                        Image(systemName: monitor.engineState == .available ? "checkmark.shield.fill" : "exclamationmark.triangle.fill")
                            .font(.title2)
                            .foregroundStyle(monitor.engineState.tint)
                        VStack(alignment: .leading, spacing: 5) {
                            Text(monitor.engineState.title).font(.headline)
                            Text(monitor.engineState.detail).font(.caption).foregroundStyle(.secondary)
                            if let contact = monitor.lastContact {
                                Text("Last read-only local check: " + contact.formatted(date: .abbreviated, time: .standard))
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        Spacer()
                        Button("Refresh", systemImage: "arrow.clockwise") { Task { await monitor.refresh() } }
                    }
                    .padding(4)
                }

                GroupBox("Service state") {
                    VStack(alignment: .leading, spacing: 9) {
                        ServiceStateRow(name: "V4", state: watchdog.status.state == "unknown" ? .unavailable : .available, detail: watchdog.status.headline)
                        ServiceStateRow(name: "FX30 bridge", state: monitor.fx30BridgeState, detail: "Read-only check: local bridge /record/status")
                        ServiceStateRow(name: "FX30 camera", state: monitor.fx30State, detail: "Requires an enabled camera with a verified warm Sony control connection")
                        ServiceStateRow(name: "OBS", state: monitor.obsState, detail: "Read-only check: OBS WebSocket Hello")
                        ServiceStateRow(name: "Companion", state: monitor.engineState == .available ? .available : .unavailable, detail: monitor.engineState.title)
                    }
                    .padding(4)
                }

                GroupBox("RØDECaster recording space") {
                    VStack(alignment: .leading, spacing: 10) {
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            StatusPill(text: monitor.rodecasterCapacity.title, color: monitor.rodecasterCapacity.tint)
                            Text(monitor.rodecasterCapacity.gateOpen ? "ARM and RECORD capacity check passed" : "BLOCKING ARM AND RECORD")
                                .font(.headline)
                                .foregroundStyle(monitor.rodecasterCapacity.gateOpen ? Color.secondary : Color.red)
                        }
                        Text(monitor.rodecasterCapacity.projectedTime + " · " + monitor.rodecasterCapacity.requiredTime)
                            .font(.title3.weight(.semibold))
                        Text(monitor.rodecasterCapacity.displayMessage)
                            .foregroundStyle(.secondary)
                        if let source = monitor.rodecasterCapacity.source, !source.isEmpty {
                            Text("Last read from: " + source)
                                .font(.caption).foregroundStyle(.secondary)
                        }

                        Divider()

                        Text("Default: 10 hours. Read the remaining time on the RØDECaster screen or official app and adjust it if needed, then confirm it here. This saves your manual check as the V4 capacity gate. It does not send a recording, camera, OBS or RØDECaster command.")
                            .font(.caption).foregroundStyle(.secondary)
                        HStack {
                            TextField("Hours remaining", text: $rodecasterHours)
                                .frame(width: 145)
                            Text("hours")
                                .foregroundStyle(.secondary)
                            TextField("Where you read it", text: $rodecasterSource)
                                .frame(minWidth: 230)
                        }
                        Button(monitor.capacityConfirmationInProgress ? "Saving capacity check…" : "Confirm RØDECaster time", systemImage: "checkmark.shield") {
                            Task {
                                await monitor.confirmRodecasterCapacity(
                                    remainingHours: rodecasterHours,
                                    source: rodecasterSource
                                )
                            }
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(monitor.capacityConfirmationInProgress || monitor.v4Recording)
                        if monitor.v4Recording {
                            Text("Capacity confirmation is locked while a recording is active.")
                                .font(.caption).foregroundStyle(.orange)
                        }
                        if let error = monitor.capacityConfirmationError {
                            Text(error).font(.caption.weight(.semibold)).foregroundStyle(.orange)
                        }
                    }
                    .padding(4)
                }

                GroupBox("Visible Page 4") {
                    VStack(alignment: .leading, spacing: 14) {
                        Text("Only these locations are eligible: ARM 4/0/2, RECORD 4/1/0 and STOP 4/1/1. A Companion reply only proves it accepted the button action.")
                            .font(.caption).foregroundStyle(.secondary)
                        HStack(spacing: 12) {
                            ControlButton(control: .arm, monitor: monitor, watchdog: watchdog.status, tint: .orange) {
                                await monitor.press(.arm, watchdog: watchdog.status)
                            }
                            ControlButton(control: .record, monitor: monitor, watchdog: watchdog.status, tint: .red) {
                                evidence.captureBaseline(using: folders)
                                await monitor.press(.record, watchdog: watchdog.status)
                            }
                            ControlButton(control: .stop, monitor: monitor, watchdog: watchdog.status, tint: .red) {
                                await monitor.press(.stop, watchdog: watchdog.status)
                            }
                        }
                        Text("StudioOS attempts a local folder baseline immediately before its RECORD request. After STOP, use Recording Folders once V4 has finished finalising the take.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    .padding(4)
                }

                GroupBox("No-camera test") {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Use this only when no FX30 is connected. First run `ARM: NO CAMERA` at 4/2/2. Then the guarded RECORD button below asks V4 to run the existing no-camera safety coordinator. StudioOS itself does not call hidden Companion routes or recorder controls.")
                            .font(.caption).foregroundStyle(.secondary)
                        ControlButton(control: .noCameraArm, monitor: monitor, watchdog: watchdog.status, tint: .orange) {
                            await monitor.press(.noCameraArm, watchdog: watchdog.status)
                        }
                        if let until = monitor.noCameraTestUntil, until > Date() {
                            Text("No-camera ARM session expires at " + until.formatted(date: .omitted, time: .shortened) + ". You can make one guarded no-camera record request before it expires.")
                                .font(.caption).foregroundStyle(.orange)
                        } else {
                            Text("ARM: NO CAMERA is a test-only readiness check. It is not production ARM and does not bypass recording safeguards.")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        HStack(spacing: 12) {
                            Button(monitor.noCameraRecordInProgress && monitor.noCameraRecord.state == "starting" ? "Starting no-camera RECORD…" : "RECORD: NO CAMERA") {
                                Task { await monitor.startNoCameraRecord() }
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(.red)
                            .disabled(!monitor.canStartNoCameraRecord)

                            Button(monitor.noCameraRecordInProgress && monitor.noCameraRecord.state == "stopping" ? "Stopping no-camera RECORD…" : "STOP: NO CAMERA") {
                                Task { await monitor.stopNoCameraRecord() }
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(.red)
                            .disabled(!monitor.canStopNoCameraRecord)
                        }
                        Text("A request being accepted is not proof of a recording. For STOP, press once only, then wait for V4 to show stopped before checking the recorded files.")
                            .font(.caption).foregroundStyle(.secondary)
                        if let detail = monitor.noCameraRecord.detail, !detail.isEmpty {
                            Text("Guard status: " + detail)
                                .font(.caption).foregroundStyle(monitor.noCameraRecord.state == "attention" ? Color.orange : Color.secondary)
                        }
                        if let message = monitor.noCameraRecordMessage {
                            Text(message).font(.caption.weight(.semibold)).foregroundStyle(.green)
                        }
                        if let error = monitor.noCameraRecordError {
                            Text(error).font(.caption.weight(.semibold)).foregroundStyle(.orange)
                        }
                        if monitor.noCameraRecordMessage != nil || monitor.noCameraRecordError != nil {
                            Button("Clear no-camera message") {
                                monitor.clearNoCameraMessages()
                            }
                            .buttonStyle(.borderless)
                        }
                    }
                    .padding(4)
                }

                GroupBox("Current state") {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(currentStateLabel).font(.title2.bold())
                        Text(watchdog.status.headline).font(.headline)
                        Text(watchdog.status.message).foregroundStyle(.secondary)
                        Text("This is V4 status evidence. It is not inferred from the button colour or the Companion reply.")
                            .font(.caption).foregroundStyle(.secondary)
                        if watchdog.canRecoverFailedStop {
                            Divider()
                            Text("Recover from failed STOP")
                                .font(.headline)
                            Text("V4 still has a stale recording/session flag, while all requested Just In channels report stopped. This does not send STOP, toggle MIDI, control OBS, cameras or media. V4 will re-check Just In, OBS and (when cameras are in use) FX30 before it can clear the stale state.")
                                .font(.callout)
                                .foregroundStyle(.secondary)
                            Toggle("I have checked that the physical RØDECaster REC light and timer are off", isOn: $physicalRodecasterConfirmedOff)
                            Button(watchdog.failedStopRecoveryInProgress ? "Recovering failed STOP…" : "Recover from failed STOP", systemImage: "checkmark.shield") {
                                Task {
                                    await watchdog.recoverFailedStop(physicalRodecasterConfirmedOff: physicalRodecasterConfirmedOff)
                                    await monitor.refresh()
                                }
                            }
                            .buttonStyle(.borderedProminent)
                            .disabled(!physicalRodecasterConfirmedOff || watchdog.failedStopRecoveryInProgress)
                            if watchdog.failedStopRecoveryInProgress {
                                ProgressView("Checking recorder stop state…")
                                    .controlSize(.small)
                            }
                            if let message = watchdog.failedStopRecoveryMessage {
                                Text(message).foregroundStyle(.green)
                            }
                            if let error = watchdog.failedStopRecoveryError {
                                Text(error).foregroundStyle(.red)
                            }
                        }
                        if watchdog.canAcknowledgeHistoricalPostRecord {
                            Divider()
                            Text("Finished testing this old warning?")
                                .font(.headline)
                            Text("This clears the old test warning so StudioOS can return to normal standby. It does not delete or change any media, and V4 refuses it if recording or post-record checks are active.")
                                .font(.callout)
                                .foregroundStyle(.secondary)
                            Button("Clear old test warning", systemImage: "checkmark.circle") {
                                Task { await watchdog.acknowledgeHistoricalPostRecord() }
                            }
                            .buttonStyle(.borderedProminent)
                            .disabled(watchdog.acknowledgementInProgress)
                        }
                        if watchdog.acknowledgementInProgress {
                            ProgressView("Clearing old test warning…")
                                .controlSize(.small)
                        }
                        if let message = watchdog.acknowledgementMessage {
                            Text(message).foregroundStyle(.green)
                        }
                        if let error = watchdog.acknowledgementError {
                            Text(error).foregroundStyle(.red)
                        }
                    }
                    .padding(4)
                }

                if let dispatch = monitor.dispatch {
                    GroupBox("Latest control receipt") {
                        VStack(alignment: .leading, spacing: 5) {
                            Text(dispatch.control.title + " · " + dispatch.sentAt.formatted(date: .abbreviated, time: .standard)).font(.headline)
                            Text(dispatch.detail).foregroundStyle(dispatch.releaseConfirmed ? Color.secondary : Color.orange)
                        }
                        .padding(4)
                    }
                }

                if let error = monitor.dispatchError {
                    Text(error).foregroundStyle(.orange).font(.callout.weight(.semibold))
                }

            }
            .padding(28)
        }
    }

    private var currentStateLabel: String {
        if monitor.activeControl == .stop { return "STOPPING" }
        if monitor.activeControl == .record { return "STARTING RECORD" }
        if monitor.activeControl == .arm { return "ARMING" }
        if monitor.activeControl == .noCameraArm { return "ARMING: NO CAMERA" }
        if monitor.noCameraRecordInProgress && monitor.noCameraRecord.state == "starting" { return "STARTING: NO CAMERA" }
        if monitor.noCameraRecordInProgress && monitor.noCameraRecord.state == "stopping" { return "STOPPING: NO CAMERA" }
        switch watchdog.status.state {
        case "recording": return "RECORDING"
        case "checking": return "POST-RECORD"
        case "green": return "COMPLETE"
        case "ready": return "NOT ARMED"
        default: return "ATTENTION"
        }
    }
}

private struct ControlButton: View {
    let control: VisiblePage4Control
    @ObservedObject var monitor: StudioControlMonitor
    let watchdog: WatchdogStatus
    let tint: Color
    let onPress: () async -> Void

    var body: some View {
        Button {
            Task { await onPress() }
        } label: {
            VStack(spacing: 5) {
                Text(control.title).font(.title3.bold())
                Text(control.rawValue).font(.caption.monospaced())
            }
            .frame(maxWidth: .infinity, minHeight: 58)
        }
        .buttonStyle(.borderedProminent)
        .tint(tint)
        .disabled(!monitor.canPress(control, watchdog: watchdog))
        .accessibilityLabel(control.title + " visible Page 4 control")
    }
}

private struct ServiceStateRow: View {
    let name: String
    let state: LocalServiceState
    let detail: String

    var body: some View {
        HStack {
            Text(name).font(.headline).frame(width: 95, alignment: .leading)
            StatusPill(text: state.label, color: state.tint)
            Text(detail).font(.caption).foregroundStyle(.secondary)
            Spacer()
        }
    }
}
