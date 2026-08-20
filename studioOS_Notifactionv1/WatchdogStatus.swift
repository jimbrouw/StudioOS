import Combine
import Foundation
import SwiftUI

struct V4ChannelStatus: Decodable {
    let name: String
    let recording: Bool?
}

struct V4PostRecordSummary: Decodable {
    let state: String
    let headline: String
    let detail: String
    let allChecksPassed: Bool
    let completedAt: String?
    let jobId: String?
    let checks: [V4PostRecordCheck]?
    let rodecasterIncluded: Bool?
}

struct V4PostRecordCheck: Decodable {
    let key: String
    let label: String
    let passed: Bool
    let count: Int?
}

struct V4FailedStopRecovery: Decodable, Equatable {
    let eligible: Bool
    let reason: String
    let staleV4Session: Bool
    let postRecordActive: Bool
}

struct V4StatusPayload: Decodable {
    let service: String
    let localOnly: Bool
    let apiReachable: Bool?
    let sourceMounted: Bool
    let recording: Bool
    let requestedChannels: [String]
    let channels: [V4ChannelStatus]?
    let postRecordSummary: V4PostRecordSummary?
    let failedStopRecovery: V4FailedStopRecovery?
    let delivery: V4DeliveryStatus?
    let error: String?
}

struct V4DeliveryStatus: Decodable, Equatable {
    let provider: String?
    let root: String?
    let justInMasters: String?
    let justInProxies: String?
    let obsMasters: String?
    let obsAudioStems: String?
    let rodecasterAudio: String?
}

struct V4OperationResponse: Decodable {
    let ok: Bool
    let error: String?
}

struct WatchdogStatus: Equatable {
    let state: String
    let headline: String
    let message: String
    let operatorActions: [String]
    let notificationIdentity: String
    let postRecord: PostRecordTask?
    let failedStopRecovery: V4FailedStopRecovery?
    let delivery: V4DeliveryStatus?

    init(
        state: String,
        headline: String,
        message: String,
        operatorActions: [String],
        notificationIdentity: String,
        postRecord: PostRecordTask? = nil,
        failedStopRecovery: V4FailedStopRecovery? = nil,
        delivery: V4DeliveryStatus? = nil
    ) {
        self.state = state
        self.headline = headline
        self.message = message
        self.operatorActions = operatorActions
        self.notificationIdentity = notificationIdentity
        self.postRecord = postRecord
        self.failedStopRecovery = failedStopRecovery
        self.delivery = delivery
    }

    static let waiting = WatchdogStatus(
        state: "unknown",
        headline: "Waiting for V4",
        message: "StudioOS is waiting for the existing local V4 studio service. This does not mean recording stopped.",
        operatorActions: [],
        notificationIdentity: "waiting-for-v4"
    )

    var isGreen: Bool { state == "green" }
    var isWarning: Bool { state == "warning" || state == "attention" || state == "unknown" }
    var tint: Color { isGreen ? .green : (isWarning ? .orange : .blue) }
    var symbol: String { isGreen ? "checkmark.shield.fill" : (isWarning ? "exclamationmark.triangle.fill" : "info.circle.fill") }

    static func fromV4(_ payload: V4StatusPayload) -> WatchdogStatus {
        guard payload.service == "fx30-just-control-v4-local" else {
            return WatchdogStatus(
                state: "warning",
                headline: "Unexpected local service",
                message: "StudioOS received a status response, but it is not the expected local V4 studio service.",
                operatorActions: ["Use the normal Page 4 controls. Check the V4 service before another take."],
                notificationIdentity: "unexpected-v4-service"
            )
        }

        if payload.recording {
            let channels = Dictionary(uniqueKeysWithValues: (payload.channels ?? []).map { ($0.name, $0.recording) })
            let allStopped = !payload.requestedChannels.isEmpty && payload.requestedChannels.allSatisfy { channels[$0] == false }
            if allStopped {
                let recovery = payload.failedStopRecovery
                return WatchdogStatus(
                    state: "attention",
                    headline: "STOP was not confirmed",
                    message: "V4 still retains the take, but every requested Just In channel now reports stopped. Confirm the physical RØDECaster REC light and timer are off before using the guarded recovery.",
                    operatorActions: ["Check OBS and, in camera mode, the FX30 status.", "Do not press Page 4 STOP again."],
                    notificationIdentity: "failed-stop-attention|" + payload.requestedChannels.joined(separator: ","),
                    failedStopRecovery: recovery
                )
            }
            let missing = payload.requestedChannels.filter { channels[$0] != true }
            if !missing.isEmpty {
                let detail = "V4 reports recording, but these expected channels are not confirmed recording: " + missing.joined(separator: ", ") + "."
                return WatchdogStatus(
                    state: "attention",
                    headline: "Recording needs checking",
                    message: detail,
                    operatorActions: ["Use the existing Page 4 STOP control if the take must end.", "Check the affected recorder and its media path."],
                    notificationIdentity: "recording-attention|" + missing.joined(separator: ",")
                )
            }
            return WatchdogStatus(
                state: "recording",
                headline: "Recording in progress",
                message: "V4 confirms all requested Just In channels are recording. StudioOS is observing only.",
                operatorActions: [],
                notificationIdentity: "recording|" + payload.requestedChannels.joined(separator: ",")
            )
        }

        if let summary = payload.postRecordSummary {
            let postRecord = PostRecordTask(
                state: summary.state,
                headline: summary.headline,
                detail: summary.detail,
                jobId: summary.jobId,
                checks: (summary.checks ?? []).map {
                    PostRecordTask.Check(key: $0.key, label: $0.label, passed: $0.passed, count: $0.count)
                },
                includesRodecaster: summary.rodecasterIncluded ?? false
            )
            if summary.state == "complete" && summary.allChecksPassed {
                let completion = summary.completedAt ?? summary.jobId ?? "latest"
                return WatchdogStatus(
                    state: "green",
                    headline: summary.headline,
                    message: summary.detail,
                    operatorActions: [],
                    notificationIdentity: "take-verified|" + completion,
                    postRecord: postRecord,
                    delivery: payload.delivery
                )
            }
            if ["attention", "failed", "cancelled"].contains(summary.state) {
                return WatchdogStatus(
                    state: "attention",
                    headline: summary.headline,
                    message: summary.detail,
                    operatorActions: ["Keep the original media in place.", "Use the V4 status and the existing Page 4 workflow to investigate."],
                    notificationIdentity: "post-record-attention|" + summary.headline + "|" + summary.detail,
                    postRecord: postRecord,
                    delivery: payload.delivery
                )
            }
            if summary.state == "running" {
                return WatchdogStatus(
                    state: "checking",
                    headline: summary.headline,
                    message: summary.detail,
                    operatorActions: ["Keep the studio and recording storage connected while V4 completes its checks."],
                    notificationIdentity: "post-record-running|" + (summary.jobId ?? "latest"),
                    postRecord: postRecord,
                    delivery: payload.delivery
                )
            }
        }

        if payload.apiReachable == false || !payload.sourceMounted {
            let detail = payload.error ?? (!payload.sourceMounted ? "The configured Just In recording folder is not mounted." : "V4 cannot reach Just In.")
            return WatchdogStatus(
                state: "warning",
                headline: "V4 needs attention",
                message: detail,
                operatorActions: ["Do not use StudioOS to control recording.", "Check V4 with the normal Page 4 ARM workflow."],
                notificationIdentity: "v4-attention|" + detail
            )
        }

        return WatchdogStatus(
            state: "ready",
            headline: "V4 standing by",
            message: "No recording is active and V4 has no completed post-record result to report yet.",
            operatorActions: [],
            notificationIdentity: "v4-standing-by",
            delivery: payload.delivery
        )
    }
}

struct PostRecordTask: Equatable {
    struct Check: Equatable, Identifiable {
        let key: String
        let label: String
        let passed: Bool
        let count: Int?

        var id: String { key }
    }

    let state: String
    let headline: String
    let detail: String
    let jobId: String?
    let checks: [Check]
    let includesRodecaster: Bool
}

@MainActor
final class WatchdogStatusMonitor: ObservableObject {
    @Published private(set) var status = WatchdogStatus.waiting
    @Published private(set) var lastContact: Date?
    @Published private(set) var contactError: String?
    @Published private(set) var acknowledgementInProgress = false
    @Published private(set) var acknowledgementMessage: String?
    @Published private(set) var acknowledgementError: String?
    @Published private(set) var failedStopRecoveryInProgress = false
    @Published private(set) var failedStopRecoveryMessage: String?
    @Published private(set) var failedStopRecoveryError: String?

    private let endpoint: URL
    private var pollingTask: Task<Void, Never>?
    private var failedAttempts = 0
    var onNotifiableChange: ((WatchdogStatus) -> Void)?

    init(endpoint: URL = URL(string: "http://127.0.0.1:3060/v4/status")!) {
        self.endpoint = endpoint
    }

    var canAcknowledgeHistoricalPostRecord: Bool {
        guard status.state == "attention", let task = status.postRecord else { return false }
        return ["attention", "failed", "cancelled"].contains(task.state)
    }

    var canRecoverFailedStop: Bool {
        status.failedStopRecovery?.eligible == true
    }

    func start() {
        guard pollingTask == nil else { return }
        pollingTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    func stop() {
        pollingTask?.cancel()
        pollingTask = nil
    }

    func refresh() async {
        do {
            var request = URLRequest(url: endpoint)
            request.cachePolicy = .reloadIgnoringLocalCacheData
            request.timeoutInterval = 3
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                throw URLError(.badServerResponse)
            }
            let payload = try JSONDecoder().decode(V4StatusPayload.self, from: data)
            let received = WatchdogStatus.fromV4(payload)
            failedAttempts = 0
            contactError = nil
            lastContact = Date()
            status = received
            notifyIfNeeded(received)
        } catch {
            failedAttempts += 1
            contactError = error.localizedDescription
            guard failedAttempts >= 2 else { return }
            let unavailable = WatchdogStatus(
                state: "unknown",
                headline: "V4 status unavailable",
                message: "StudioOS cannot reach the existing local V4 studio service. This does not confirm that recording stopped.",
                operatorActions: ["Use the normal Page 4 controls independently.", "Check the V4 service before another take."],
                notificationIdentity: "v4-status-unavailable"
            )
            status = unavailable
            notifyIfNeeded(unavailable)
        }
    }

    /// Acknowledges only an old, terminal V4 post-record receipt. V4 refuses
    /// this request while recording or while post-record work is still active.
    /// It does not retry, move, delete, or rewrite media.
    func acknowledgeHistoricalPostRecord() async {
        guard canAcknowledgeHistoricalPostRecord, !acknowledgementInProgress else { return }

        acknowledgementInProgress = true
        acknowledgementMessage = nil
        acknowledgementError = nil
        defer { acknowledgementInProgress = false }

        do {
            let acknowledgementURL = endpoint
                .deletingLastPathComponent()
                .appendingPathComponent("post-record/acknowledge")
            var request = URLRequest(url: acknowledgementURL)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = Data("{}".utf8)
            request.timeoutInterval = 5

            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                throw URLError(.badServerResponse)
            }
            let result = try JSONDecoder().decode(V4OperationResponse.self, from: data)
            guard result.ok else {
                throw NSError(
                    domain: "StudioOS",
                    code: 1,
                    userInfo: [NSLocalizedDescriptionKey: result.error ?? "V4 did not acknowledge the old post-record warning."]
                )
            }

            acknowledgementMessage = "Old test warning cleared. No media was changed."
            await refresh()
        } catch {
            acknowledgementError = "StudioOS could not clear the old test warning: " + error.localizedDescription
        }
    }

    /// This does not retry STOP or control any recorder. V4 performs fresh,
    /// read-only Just In/OBS/FX30 checks and rejects ambiguous status.
    func recoverFailedStop(physicalRodecasterConfirmedOff: Bool) async {
        guard canRecoverFailedStop, !failedStopRecoveryInProgress else { return }
        guard physicalRodecasterConfirmedOff else {
            failedStopRecoveryError = "Confirm the physical RØDECaster REC light and timer are off before recovery."
            return
        }

        failedStopRecoveryInProgress = true
        failedStopRecoveryMessage = nil
        failedStopRecoveryError = nil
        defer { failedStopRecoveryInProgress = false }
        do {
            let recoveryURL = endpoint
                .deletingLastPathComponent()
                .appendingPathComponent("record/recover-failed-stop")
            var request = URLRequest(url: recoveryURL)
            request.httpMethod = "POST"
            request.timeoutInterval = 8
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: [
                "rodecasterConfirmedOff": true,
                "rodecasterConfirmationSource": "StudioOS operator confirmed physical REC light and timer off"
            ])
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                throw URLError(.badServerResponse)
            }
            let result = try JSONDecoder().decode(V4OperationResponse.self, from: data)
            guard result.ok else {
                throw NSError(domain: "StudioOS", code: 1, userInfo: [NSLocalizedDescriptionKey: result.error ?? "V4 did not complete failed STOP recovery."])
            }
            failedStopRecoveryMessage = "Failed STOP recovery completed. Enter a fresh RØDECaster capacity reading before another take."
            await refresh()
        } catch {
            failedStopRecoveryError = "Failed STOP recovery was not completed: " + error.localizedDescription
            await refresh()
        }
    }

    private func notifyIfNeeded(_ received: WatchdogStatus) {
        guard received.isWarning || received.state == "green" else { return }
        onNotifiableChange?(received)
    }
}
