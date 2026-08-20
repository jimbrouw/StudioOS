//
//  ContentView.swift
//  studioOS_Notifactionv1
//

import SwiftUI
import AppKit
@preconcurrency import UserNotifications
import Combine
import OSLog

enum AlertPriority: String, CaseIterable, Codable, Identifiable {
    case info = "Info"
    case attention = "Attention"
    case urgent = "Urgent"

    var id: String { rawValue }
    var tint: Color {
        switch self {
        case .info: .blue
        case .attention: .orange
        case .urgent: .red
        }
    }
}

enum AlertState: String, CaseIterable, Codable, Identifiable {
    case active = "Active"
    case acknowledged = "Acknowledged"
    case resolved = "Resolved"

    var id: String { rawValue }
}

enum RecipientGroup: String, CaseIterable, Codable, Identifiable {
    case producer = "Producer"
    case edit = "Edit"
    case camera = "Camera"
    case everyone = "Everyone"

    var id: String { rawValue }
}

struct StudioAlert: Identifiable, Codable, Equatable {
    var id = UUID()
    var title: String
    var message: String
    var priority: AlertPriority
    var recipient: RecipientGroup
    var sender: String
    var timestamp = Date()
    var state: AlertState = .active
}

@MainActor
final class AlertStore: ObservableObject {
    @Published private(set) var alerts: [StudioAlert] = [] {
        didSet { save() }
    }
    private let storageKey = "studioOS.localAlerts.v1"

    init() {
        guard let data = UserDefaults.standard.data(forKey: storageKey),
              let saved = try? JSONDecoder().decode([StudioAlert].self, from: data) else { return }
        alerts = saved
    }

    var activeAlerts: [StudioAlert] { alerts.filter { $0.state != .resolved } }
    var unresolvedUrgentCount: Int { alerts.filter { $0.priority == .urgent && $0.state != .resolved }.count }

    func add(_ alert: StudioAlert) { alerts.insert(alert, at: 0) }
    func updateState(_ alert: StudioAlert, to state: AlertState) {
        guard let index = alerts.firstIndex(of: alert) else { return }
        alerts[index].state = state
    }
    func clearResolved() { alerts.removeAll { $0.state == .resolved } }

    private func save() {
        guard let data = try? JSONEncoder().encode(alerts) else { return }
        UserDefaults.standard.set(data, forKey: storageKey)
    }
}

@MainActor
final class LocalNotificationManager: ObservableObject {
    @Published private(set) var permissionStatus: UNAuthorizationStatus = .notDetermined
    @Published private(set) var permissionError: String?
    private let logger = Logger(subsystem: "jimbrouwer.studioOS-Notifactionv1", category: "watchdog-notifications")
    private var lastWatchdogNotificationKey: String?

    init() { refreshStatus() }

    func refreshStatus(requestIfNeeded: Bool = false) {
        UNUserNotificationCenter.current().getNotificationSettings { [weak self] settings in
            DispatchQueue.main.async {
                self?.permissionStatus = settings.authorizationStatus
                self?.logger.info("Notification authorization status: \(settings.authorizationStatus.rawValue)")
                if requestIfNeeded && settings.authorizationStatus == .notDetermined {
                    self?.requestPermission()
                }
            }
        }
    }

    func requestPermission() {
        let center = UNUserNotificationCenter.current()
        permissionError = nil
        center.requestAuthorization(options: [.alert, .badge, .sound]) { [weak self] _, error in
            if let error {
                DispatchQueue.main.async {
                    self?.permissionError = "macOS could not request notification permission: " + error.localizedDescription
                }
            }
            center.getNotificationSettings { settings in
                DispatchQueue.main.async {
                    self?.permissionStatus = settings.authorizationStatus
                    if settings.authorizationStatus == .denied {
                        self?.permissionError = "Notifications are blocked for StudioOS in System Settings."
                    }
                }
            }
        }
    }

    func sendTest(title: String = "StudioOS local notification test", body: String = "This test was created on this Mac. It has no external trigger.") {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        permissionError = nil
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)) { [weak self] error in
            guard let error else { return }
            DispatchQueue.main.async {
                self?.permissionError = "macOS rejected the notification test: " + error.localizedDescription
            }
        }
    }

    func sendWatchdogStatus(_ status: WatchdogStatus) {
        guard permissionStatus == .authorized else { return }
        // Contact-loss status is regenerated on every failed poll, so its
        // timestamp must not create a fresh notification every two seconds.
        // Real watchdog events keep their persisted updatedAt as the event key.
        let notificationKey = status.notificationIdentity
        guard notificationKey != lastWatchdogNotificationKey else { return }
        lastWatchdogNotificationKey = notificationKey
        let content = UNMutableNotificationContent()
        content.title = status.headline
        content.body = status.message
        content.sound = .default
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(
                identifier: "studioos.watchdog.\(status.notificationIdentity)",
                content: content,
                trigger: nil
            )
        ) { [logger] error in
            if let error {
                logger.error("Watchdog notification rejected: \(error.localizedDescription, privacy: .public)")
            } else {
                logger.info("Watchdog notification accepted for display: \(status.headline, privacy: .public)")
            }
        }
    }

    var statusLabel: String {
        switch permissionStatus {
        case .authorized, .provisional, .ephemeral: "Allowed"
        case .denied: "Blocked in System Settings"
        case .notDetermined: "Not requested"
        @unknown default: "Unknown"
        }
    }
}

struct ContentView: View {
    @StateObject private var store = AlertStore()
    @StateObject private var notifications = LocalNotificationManager()
    @StateObject private var watchdog = WatchdogStatusMonitor()
    @StateObject private var studioControl = StudioControlMonitor()
    @StateObject private var recordingFolders = RecordingFolderAccessStore()
    @StateObject private var recordingEvidence = RecordingEvidenceStore()
    @State private var selectedSection = "Installation"

    var body: some View {
        NavigationSplitView {
            List(selection: $selectedSection) {
                Section("CONTROL ROOM") {
                    Label("Installation", systemImage: "shippingbox.and.arrow.backward")
                        .tag("Installation")
                    Label("Studio Control", systemImage: "switch.2")
                        .tag("Studio Control")
                    Label("FX30 Control Room", systemImage: "video.badge.ellipsis")
                        .tag("FX30 Control Room")
                    Label("Recording Status", systemImage: "waveform.path.ecg.rectangle")
                        .tag("Recording Status")
                    Label("Recording Folders", systemImage: "folder.badge.gearshape")
                        .tag("Recording Folders")
                    Label("Alert Desk", systemImage: "exclamationmark.bubble")
                        .tag("Alert Desk")
                    Label("Alert History", systemImage: "clock.arrow.circlepath")
                        .tag("Alert History")
                }
                Section("LOCAL DELIVERY") {
                    Label("Notification Test", systemImage: "bell.badge")
                        .tag("Notification Test")
                }
            }
            .navigationTitle("StudioOS")
            .frame(minWidth: 190)
        } detail: {
            switch selectedSection {
            case "Installation": InstallationSetupView(folders: recordingFolders, notifications: notifications)
            case "FX30 Control Room": FX30ControlRoomView()
            case "Studio Control": StudioControlView(monitor: studioControl, watchdog: watchdog, folders: recordingFolders, evidence: recordingEvidence)
            case "Recording Status": WatchdogStatusView(monitor: watchdog, notifications: notifications)
            case "Recording Folders": RecordingFolderAccessView(store: recordingFolders, evidence: recordingEvidence)
            case "Alert History": AlertHistoryView(store: store)
            case "Notification Test": NotificationTestView(notifications: notifications)
            default: AlertDeskView(store: store, notifications: notifications)
            }
        }
        .frame(minWidth: 980, minHeight: 650)
        .task {
            watchdog.onNotifiableChange = { status in notifications.sendWatchdogStatus(status) }
            watchdog.start()
            studioControl.start()
        }
        .onDisappear {
            watchdog.stop()
            studioControl.stopPolling()
            studioControl.clearNoCameraTestSession()
        }
    }
}

private struct WatchdogStatusView: View {
    @ObservedObject var monitor: WatchdogStatusMonitor
    @ObservedObject var notifications: LocalNotificationManager

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text("Recording Status").font(.largeTitle.bold())
                        Text("Read-only display for the existing V4 studio service. StudioOS cannot record, stop, change storage, control cameras, or recover media.")
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text("V4 OBSERVER · 3060")
                        .font(.caption.bold())
                        .padding(.horizontal, 10).padding(.vertical, 6)
                        .background(.orange.opacity(0.15), in: Capsule())
                        .foregroundStyle(.orange)
                }

                GroupBox {
                    HStack(alignment: .top, spacing: 18) {
                        Image(systemName: monitor.status.symbol)
                            .font(.system(size: 38))
                            .foregroundStyle(monitor.status.tint)
                        VStack(alignment: .leading, spacing: 8) {
                            Text(monitor.status.headline).font(.title2.bold())
                            Text(monitor.status.message).font(.body)
                            Text("Source: local V4 studio service · port 3060")
                                .font(.caption).foregroundStyle(.secondary)
                            if let contact = monitor.lastContact {
                                Text("Last local contact: \(contact.formatted(date: .abbreviated, time: .standard))")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        Spacer()
                        StatusPill(
                            text: monitor.status.isGreen ? "GREEN" : (monitor.status.isWarning ? "WARNING" : "INFO"),
                            color: monitor.status.tint
                        )
                    }
                    .padding(8)
                }

                if !monitor.status.operatorActions.isEmpty {
                    GroupBox("What to do") {
                        VStack(alignment: .leading, spacing: 10) {
                            ForEach(Array(monitor.status.operatorActions.enumerated()), id: \.offset) { index, action in
                                HStack(alignment: .top) {
                                    Text("\(index + 1).").bold()
                                    Text(action)
                                }
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(4)
                    }
                }

                GroupBox("After STOP: delivery task") {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("V4 waits for the Just In files to settle, checks the masters on the Extreme SSD, creates playable proxies, then copies and verifies the Just In masters and proxies in LucidLink.")
                            .fixedSize(horizontal: false, vertical: true)

                        Text("It does not delete media or control OBS, cameras, or the RØDECaster. OBS masters, stems, and RØDECaster audio remain separately visible delivery stages until their source routes are connected.")
                            .font(.callout.weight(.semibold))
                            .foregroundStyle(.secondary)

                        if let delivery = monitor.status.delivery {
                            Divider()
                            Text((delivery.provider ?? "Delivery") + " destination")
                                .font(.headline)
                            ForEach([
                                ("Just In masters", delivery.justInMasters),
                                ("Just In proxies", delivery.justInProxies),
                                ("OBS masters", delivery.obsMasters),
                                ("OBS audio stems", delivery.obsAudioStems),
                                ("RØDECaster audio", delivery.rodecasterAudio)
                            ], id: \.0) { label, path in
                                if let path {
                                    Text(label + ": " + path)
                                        .font(.caption.monospaced())
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }

                        if let task = monitor.status.postRecord {
                            Divider()
                            HStack(alignment: .firstTextBaseline) {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(task.headline).font(.headline)
                                    Text(task.detail).font(.callout).foregroundStyle(.secondary)
                                }
                                Spacer()
                                StatusPill(
                                    text: task.state == "complete" ? "PASSED" : (task.state == "running" ? "RUNNING" : "ATTENTION"),
                                    color: task.state == "complete" ? .green : (task.state == "running" ? .blue : .orange)
                                )
                            }

                            if let jobId = task.jobId {
                                Text("Task: \(jobId)")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }

                            ForEach(task.checks) { check in
                                HStack(alignment: .top, spacing: 8) {
                                    Image(systemName: check.passed ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                                        .foregroundStyle(check.passed ? .green : .orange)
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(check.label)
                                        if let count = check.count {
                                            Text("\(count) file\(count == 1 ? "" : "s") checked")
                                                .font(.caption)
                                                .foregroundStyle(.secondary)
                                        }
                                    }
                                    Spacer()
                                    Text(check.passed ? "Passed" : "Needs attention")
                                        .font(.caption.weight(.semibold))
                                        .foregroundStyle(check.passed ? .green : .orange)
                                }
                            }

                            if !task.includesRodecaster {
                                Text("RØDECaster archiving is not included in this task.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }

                        } else {
                            Text("No finished post-record task has been reported by V4 yet.")
                                .font(.callout)
                                .foregroundStyle(.secondary)
                        }

                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(4)
                }

                HStack {
                    Button("Refresh status", systemImage: "arrow.clockwise") { Task { await monitor.refresh() } }
                    Spacer()
                    Text("Notifications: \(notifications.statusLabel)")
                        .font(.caption).foregroundStyle(.secondary)
                    if notifications.permissionStatus == .notDetermined {
                        Button("Allow notifications") { notifications.requestPermission() }
                    }
                }

                Text("A green result means V4 reports its local post-record checks passed. StudioOS does not control Page 4 or independently prove editor delivery.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .padding(28)
        }
    }
}

private struct AlertDeskView: View {
    @ObservedObject var store: AlertStore
    @ObservedObject var notifications: LocalNotificationManager
    @State private var showingComposer = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text("Alert Desk").font(.largeTitle.bold())
                        Text("Local alert board. Watchdog status is displayed separately and remains read-only.")
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    UrgentBadge(count: store.unresolvedUrgentCount)
                    Button("Compose alert", systemImage: "plus") { showingComposer = true }
                        .buttonStyle(.borderedProminent)
                }

                GroupBox("Preset alerts") {
                    HStack(spacing: 10) {
                        ForEach(presets) { preset in
                            Button { create(preset) } label: {
                                VStack(alignment: .leading, spacing: 5) {
                                    Image(systemName: preset.symbol).font(.title3)
                                    Text(preset.title).font(.subheadline.weight(.semibold)).multilineTextAlignment(.leading)
                                }
                                .frame(maxWidth: .infinity, alignment: .leading)
                            }
                            .buttonStyle(.bordered)
                            .tint(preset.priority.tint)
                        }
                    }
                    .padding(.vertical, 4)
                }

                HStack {
                    Text("Active board").font(.title2.bold())
                    Text("\(store.activeAlerts.count) open").foregroundStyle(.secondary)
                    Spacer()
                    if notifications.permissionStatus != .authorized {
                        Text("Notifications: \(notifications.statusLabel)").font(.caption).foregroundStyle(.secondary)
                    }
                }

                if store.activeAlerts.isEmpty {
                    ContentUnavailableView("No active alerts", systemImage: "checkmark.circle", description: Text("Create a preset or custom alert when the control room needs attention."))
                        .frame(maxWidth: .infinity, minHeight: 210)
                        .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 12))
                } else {
                    LazyVStack(spacing: 10) {
                        ForEach(store.activeAlerts) { alert in
                            AlertCard(alert: alert, store: store)
                        }
                    }
                }
            }
            .padding(28)
        }
        .sheet(isPresented: $showingComposer) {
            AlertComposer { alert in
                store.add(alert)
                if notifications.permissionStatus == .authorized { notifications.sendTest(title: alert.title, body: alert.message) }
            }
        }
    }

    private func create(_ preset: AlertPreset) {
        let alert = StudioAlert(title: preset.title, message: preset.message, priority: preset.priority, recipient: preset.recipient, sender: "Control room")
        store.add(alert)
        if notifications.permissionStatus == .authorized { notifications.sendTest(title: alert.title, body: alert.message) }
    }
}

private struct AlertCard: View {
    let alert: StudioAlert
    @ObservedObject var store: AlertStore

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Circle().fill(alert.priority.tint).frame(width: 10, height: 10).padding(.top, 6)
            VStack(alignment: .leading, spacing: 7) {
                HStack {
                    Text(alert.title).font(.headline)
                    StatusPill(text: alert.priority.rawValue, color: alert.priority.tint)
                    StatusPill(text: alert.state.rawValue, color: alert.state == .acknowledged ? .yellow : .secondary)
                }
                Text(alert.message).foregroundStyle(.secondary)
                Text("To: \(alert.recipient.rawValue)  •  From: \(alert.sender)  •  \(alert.timestamp.formatted(date: .abbreviated, time: .shortened))")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 8) {
                if alert.state == .active { Button("Acknowledge") { store.updateState(alert, to: .acknowledged) } }
                Button("Resolve") { store.updateState(alert, to: .resolved) }.tint(.green)
            }
            .buttonStyle(.bordered)
        }
        .padding(16)
        .background(alert.priority.tint.opacity(0.07), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(alert.priority.tint.opacity(0.25)))
    }
}

private struct AlertHistoryView: View {
    @ObservedObject var store: AlertStore
    @State private var search = ""
    @State private var stateFilter: AlertState?

    private var filtered: [StudioAlert] {
        store.alerts.filter { alert in
            (search.isEmpty || [alert.title, alert.message, alert.sender, alert.recipient.rawValue].joined(separator: " ").localizedCaseInsensitiveContains(search)) &&
            (stateFilter == nil || alert.state == stateFilter)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                VStack(alignment: .leading) {
                    Text("Alert History").font(.largeTitle.bold())
                    Text("Stored on this Mac only.").foregroundStyle(.secondary)
                }
                Spacer()
                Button("Clear resolved") { store.clearResolved() }.disabled(!store.alerts.contains { $0.state == .resolved })
            }
            HStack {
                TextField("Search title, message, sender, or group", text: $search).textFieldStyle(.roundedBorder)
                Picker("State", selection: $stateFilter) {
                    Text("All states").tag(AlertState?.none)
                    ForEach(AlertState.allCases) { Text($0.rawValue).tag(AlertState?.some($0)) }
                }.frame(width: 180)
            }
            Table(filtered) {
                TableColumn("Alert") { Text($0.title) }
                TableColumn("Priority") { StatusPill(text: $0.priority.rawValue, color: $0.priority.tint) }
                TableColumn("To") { Text($0.recipient.rawValue) }
                TableColumn("Sender") { Text($0.sender) }
                TableColumn("State") { Text($0.state.rawValue) }
                TableColumn("Time") { Text($0.timestamp.formatted(date: .abbreviated, time: .shortened)) }
            }
        }
        .padding(28)
    }
}

private struct NotificationTestView: View {
    @ObservedObject var notifications: LocalNotificationManager
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text("Local Notification Test").font(.largeTitle.bold())
                Text("This manual test checks macOS notification delivery. Automatic watchdog alerts are read-only and never send recording or recovery commands.")
                    .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                GroupBox("macOS permission") {
                    HStack {
                        VStack(alignment: .leading) {
                            Text(notifications.statusLabel).font(.headline)
                            Text("macOS controls whether banners and sounds can appear.").font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("Refresh status") { notifications.refreshStatus() }
                        if notifications.permissionStatus == .notDetermined {
                            Button("Allow notifications") { notifications.requestPermission() }
                                .buttonStyle(.borderedProminent)
                        }
                    }
                    .padding(4)
                }
                Button("Send local notification test", systemImage: "bell") { notifications.sendTest() }
                    .buttonStyle(.borderedProminent)
                    .disabled(notifications.permissionStatus != .authorized)
                if let error = notifications.permissionError {
                    Text(error)
                        .font(.callout.weight(.semibold))
                        .foregroundStyle(.orange)
                } else if notifications.permissionStatus != .authorized {
                    Text("Allow notifications first. If macOS has blocked them, enable StudioOS Notifications in System Settings > Notifications, then refresh this status.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(28)
        }
    }
}

private struct AlertComposer: View {
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var message = ""
    @State private var priority: AlertPriority = .attention
    @State private var recipient: RecipientGroup = .producer
    let onCreate: (StudioAlert) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Compose local alert").font(.title2.bold())
            TextField("Title", text: $title)
            TextField("Message", text: $message, axis: .vertical).lineLimit(3...6)
            Picker("Priority", selection: $priority) { ForEach(AlertPriority.allCases) { Text($0.rawValue).tag($0) } }
            Picker("Send to", selection: $recipient) { ForEach(RecipientGroup.allCases) { Text($0.rawValue).tag($0) } }
            HStack { Spacer(); Button("Cancel") { dismiss() }; Button("Create alert") { onCreate(StudioAlert(title: title, message: message, priority: priority, recipient: recipient, sender: "Control room")); dismiss() }.buttonStyle(.borderedProminent).disabled(title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) }
        }
        .padding(24).frame(width: 460)
    }
}

struct StatusPill: View {
    let text: String
    let color: Color
    var body: some View { Text(text).font(.caption.weight(.semibold)).padding(.horizontal, 7).padding(.vertical, 3).background(color.opacity(0.16), in: Capsule()).foregroundStyle(color) }
}

private struct UrgentBadge: View {
    let count: Int
    var body: some View { Label(count == 0 ? "No urgent alerts" : "\(count) urgent unresolved", systemImage: count == 0 ? "checkmark.shield" : "exclamationmark.triangle.fill").font(.subheadline.weight(.semibold)).foregroundStyle(count == 0 ? Color.secondary : Color.red).padding(.horizontal, 10).padding(.vertical, 7).background((count == 0 ? Color.secondary : Color.red).opacity(0.10), in: Capsule()) }
}

private struct AlertPreset: Identifiable {
    let title: String; let message: String; let priority: AlertPriority; let recipient: RecipientGroup; let symbol: String
    var id: String { title }
}

private let presets: [AlertPreset] = [
    .init(title: "Camera card full", message: "Camera media card needs attention before the next record.", priority: .urgent, recipient: .camera, symbol: "memorychip"),
    .init(title: "Recording stopped", message: "Check the record path and confirm the next take is captured.", priority: .urgent, recipient: .producer, symbol: "record.circle"),
    .init(title: "Media ready to ingest", message: "Media is ready for the edit desk to ingest.", priority: .info, recipient: .edit, symbol: "tray.and.arrow.down"),
    .init(title: "Client review ready", message: "A client review is ready for the producer to check.", priority: .attention, recipient: .producer, symbol: "eye"),
    .init(title: "Action needed", message: "The control room needs a decision or follow-up.", priority: .attention, recipient: .everyone, symbol: "hand.raised")
]
