import AppKit
import Combine
import Foundation
import SwiftUI

enum RecordingFolderRole: String, CaseIterable, Identifiable, Codable {
    case justInMasters
    case obsMasters
    case obsStems
    case proxies
    case rodecasterAudio

    var id: String { rawValue }

    var title: String {
        switch self {
        case .justInMasters: "Just In masters"
        case .obsMasters: "OBS masters"
        case .obsStems: "OBS audio stems"
        case .proxies: "Just In proxies"
        case .rodecasterAudio: "RØDECaster audio"
        }
    }

    var detail: String {
        switch self {
        case .justInMasters: "The folder containing the three Just In camera masters."
        case .obsMasters: "The folder containing OBS programme masters."
        case .obsStems: "The folder containing OBS audio stem files."
        case .proxies: "The folder where verified Just In proxies are written."
        case .rodecasterAudio: "The folder containing imported RØDECaster audio."
        }
    }
}

struct RecordingFolderAccess: Codable, Equatable {
    let bookmark: Data
    let displayPath: String
}

struct RecordingFolderItem: Codable, Equatable, Identifiable {
    let name: String
    let byteCount: Int64
    let modifiedAt: Date

    var id: String { name }
}

struct RecordingMediaProbe: Codable, Equatable, Identifiable {
    let role: RecordingFolderRole
    let item: RecordingFolderItem
    let sizeIsStable: Bool
    let isMediaReadable: Bool

    var id: String { role.rawValue + "/" + item.name }
}

enum RecordingFolderCheck: Equatable {
    case notChosen
    case ready(itemCount: Int)
    case needsSelection(String)

    var title: String {
        switch self {
        case .notChosen: "Not chosen"
        case .ready: "Access confirmed"
        case .needsSelection: "Needs attention"
        }
    }

    var detail: String {
        switch self {
        case .notChosen: "Choose this folder once to allow StudioOS to check it after future takes."
        case .ready(let itemCount): "StudioOS can read this folder. " + String(itemCount) + " item" + (itemCount == 1 ? "" : "s") + " currently visible."
        case .needsSelection(let message): message
        }
    }

    var color: Color {
        switch self {
        case .ready: .green
        case .notChosen: .secondary
        case .needsSelection: .orange
        }
    }
}

@MainActor
final class RecordingFolderAccessStore: ObservableObject {
    @Published private(set) var folders: [RecordingFolderRole: RecordingFolderAccess] = [:]
    @Published private(set) var checks: [RecordingFolderRole: RecordingFolderCheck] = [:]

    private let storageKey = "studioOS.recordingFolders.v1"

    init() {
        load()
        for role in RecordingFolderRole.allCases {
            checks[role] = folders[role] == nil ? .notChosen : .needsSelection("Access has not been checked in this StudioOS session.")
        }
        // Saved security-scoped bookmarks are meant to survive app launches.
        // Recheck them immediately so a reopened StudioOS shows the existing
        // recording locations as ready, unless the drive was disconnected or
        // macOS genuinely needs the operator to choose a replacement.
        checkAll()
    }

    func chooseFolder(for role: RecordingFolderRole) {
        let panel = NSOpenPanel()
        panel.title = "Choose " + role.title + " folder"
        panel.message = "StudioOS will receive read-only access to this folder for future recording checks."
        panel.prompt = "Use this folder"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = false

        guard panel.runModal() == .OK, let url = panel.url else { return }

        do {
            let bookmark = try url.bookmarkData(
                options: [.withSecurityScope, .securityScopeAllowOnlyReadAccess],
                includingResourceValuesForKeys: nil,
                relativeTo: nil
            )
            folders[role] = RecordingFolderAccess(bookmark: bookmark, displayPath: url.path)
            save()
            check(role)
        } catch {
            checks[role] = .needsSelection("StudioOS could not save access to this folder: " + error.localizedDescription)
        }
    }

    func displayPath(for role: RecordingFolderRole) -> String? {
        folders[role]?.displayPath
    }

    func isReady(_ role: RecordingFolderRole) -> Bool {
        if case .ready = checks[role] { return true }
        return false
    }

    func checkAll() {
        RecordingFolderRole.allCases.forEach(check)
    }

    func clearFolder(for role: RecordingFolderRole) {
        folders.removeValue(forKey: role)
        checks[role] = .notChosen
        save()
    }

    func snapshot(for role: RecordingFolderRole) throws -> [RecordingFolderItem] {
        guard let access = folders[role] else { throw FolderSnapshotError.notChosen }
        var isStale = false
        let url = try URL(
            resolvingBookmarkData: access.bookmark,
            options: [.withSecurityScope],
            relativeTo: nil,
            bookmarkDataIsStale: &isStale
        )
            if isStale {
                try refreshBookmark(for: role, url: url)
            }
        guard url.startAccessingSecurityScopedResource() else { throw FolderSnapshotError.denied }
        defer { url.stopAccessingSecurityScopedResource() }

        let keys: Set<URLResourceKey> = [.isRegularFileKey, .fileSizeKey, .contentModificationDateKey]
        return try FileManager.default.contentsOfDirectory(at: url, includingPropertiesForKeys: Array(keys), options: [.skipsHiddenFiles])
            .compactMap { item in
                let values = try? item.resourceValues(forKeys: keys)
                guard values?.isRegularFile == true,
                      let byteCount = values?.fileSize,
                      let modifiedAt = values?.contentModificationDate else { return nil }
                return RecordingFolderItem(name: item.lastPathComponent, byteCount: Int64(byteCount), modifiedAt: modifiedAt)
            }
            .sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
    }

    func probeMedia(_ items: [RecordingFolderItem], in role: RecordingFolderRole) async throws -> [RecordingMediaProbe] {
        guard let access = folders[role] else { throw FolderSnapshotError.notChosen }
        var isStale = false
        let folderURL = try URL(
            resolvingBookmarkData: access.bookmark,
            options: [.withSecurityScope],
            relativeTo: nil,
            bookmarkDataIsStale: &isStale
        )
            if isStale {
                try refreshBookmark(for: role, url: folderURL)
            }
        guard folderURL.startAccessingSecurityScopedResource() else { throw FolderSnapshotError.denied }
        defer { folderURL.stopAccessingSecurityScopedResource() }

        let initialSizes = Dictionary(uniqueKeysWithValues: items.map { ($0.name, $0.byteCount) })
        try await Task.sleep(for: .seconds(3))
        return await items.asyncMap { item in
            let url = folderURL.appendingPathComponent(item.name)
            let currentSize = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize).map(Int64.init)
            let sizeIsStable = currentSize == initialSizes[item.name]
            let isMediaReadable = await MediaReadability.canRead(url)
            return RecordingMediaProbe(role: role, item: item, sizeIsStable: sizeIsStable, isMediaReadable: isMediaReadable)
        }
    }

    private func check(_ role: RecordingFolderRole) {
        guard let access = folders[role] else {
            checks[role] = .notChosen
            return
        }

        do {
            var isStale = false
            let url = try URL(
                resolvingBookmarkData: access.bookmark,
                options: [.withSecurityScope],
                relativeTo: nil,
                bookmarkDataIsStale: &isStale
            )
            if isStale {
                try refreshBookmark(for: role, url: url)
            }
            guard url.startAccessingSecurityScopedResource() else {
                checks[role] = .needsSelection("macOS did not grant StudioOS access. Choose this folder again.")
                return
            }
            defer { url.stopAccessingSecurityScopedResource() }

            var isDirectory: ObjCBool = false
            guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory), isDirectory.boolValue else {
                checks[role] = .needsSelection("This folder is unavailable. Reconnect the recording drive, then check again.")
                return
            }
            let items = try FileManager.default.contentsOfDirectory(atPath: url.path)
            checks[role] = .ready(itemCount: items.count)
        } catch {
            checks[role] = .needsSelection("StudioOS could not read this folder: " + error.localizedDescription)
        }
    }

    private func load() {
        guard let data = UserDefaults.standard.data(forKey: storageKey),
              let saved = try? JSONDecoder().decode([RecordingFolderRole: RecordingFolderAccess].self, from: data) else { return }
        folders = saved
    }

    private func save() {
        guard let data = try? JSONEncoder().encode(folders) else { return }
        UserDefaults.standard.set(data, forKey: storageKey)
    }

    private func refreshBookmark(for role: RecordingFolderRole, url: URL) throws {
        let bookmark = try url.bookmarkData(
            options: [.withSecurityScope, .securityScopeAllowOnlyReadAccess],
            includingResourceValuesForKeys: nil,
            relativeTo: nil
        )
        folders[role] = RecordingFolderAccess(bookmark: bookmark, displayPath: url.path)
        save()
    }
}

private extension Collection {
    func asyncMap<T>(_ transform: @escaping (Element) async -> T) async -> [T] {
        var results: [T] = []
        for element in self { results.append(await transform(element)) }
        return results
    }
}

private enum FolderSnapshotError: LocalizedError {
    case notChosen
    case stale
    case denied

    var errorDescription: String? {
        switch self {
        case .notChosen: "No folder has been selected."
        case .stale: "This folder permission is stale. Choose the folder again."
        case .denied: "macOS did not grant StudioOS access to this folder."
        }
    }
}

struct RecordingFolderAccessView: View {
    @ObservedObject var store: RecordingFolderAccessStore
    @ObservedObject var evidence: RecordingEvidenceStore

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Recording Folders").font(.largeTitle.bold())
                    Text("Choose each recording folder once. StudioOS keeps read-only access so it can check future takes without Terminal or Full Disk Access changes.")
                        .foregroundStyle(.secondary)
                }

                GroupBox("Before you start") {
                    Text("This screen cannot start or stop recorders, change a destination, or inspect media contents. “Check access” only confirms that StudioOS can open and list the selected folders.")
                        .foregroundStyle(.secondary)
                        .padding(4)
                }

                ForEach(RecordingFolderRole.allCases) { role in
                    FolderAccessRow(role: role, access: store.folders[role], check: store.checks[role] ?? .notChosen, choose: { store.chooseFolder(for: role) }, clear: { store.clearFolder(for: role) })
                }

                HStack {
                    Button("Check folder access", systemImage: "checkmark.shield") { store.checkAll() }
                        .buttonStyle(.borderedProminent)
                    Text("All three must show Access confirmed before the future watchdog can use them.")
                        .font(.caption).foregroundStyle(.secondary)
                }

                GroupBox("Read-only evidence rehearsal") {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Capture a baseline before a future test take. Afterwards, StudioOS can identify file names that appeared after that point. These controls never start or stop a recorder.")
                            .foregroundStyle(.secondary)
                        HStack {
                            Button("Capture baseline", systemImage: "camera.metering.center.weighted") {
                                evidence.captureBaseline(using: store)
                            }
                            Button("Check for new files", systemImage: "doc.badge.plus") {
                                evidence.compareWithBaseline(using: store)
                            }
                            .disabled(evidence.baseline == nil)
                            Button("Verify new media", systemImage: "checkmark.seal") {
                                Task { await evidence.verifyNewMedia(using: store) }
                            }
                            .disabled(evidence.lastSummary == nil || evidence.lastSummary?.totalNewItems == 0)
                        }
                        .buttonStyle(.bordered)
                        if let baseline = evidence.baseline {
                            Text("Baseline captured: " + baseline.capturedAt.formatted(date: .abbreviated, time: .standard))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        if let summary = evidence.lastSummary {
                            Text(summary.totalNewItems == 0 ? "No new files were found after this baseline." : String(summary.totalNewItems) + " new files found after this baseline.")
                                .font(.subheadline.weight(.semibold))
                            ForEach(RecordingFolderRole.allCases) { role in
                                Text(role.title + ": " + String((summary.newItems[role] ?? []).count) + " new file(s)")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        if !evidence.mediaProbes.isEmpty {
                            let stableCount = evidence.mediaProbes.filter(\.sizeIsStable).count
                            let readableCount = evidence.mediaProbes.filter(\.isMediaReadable).count
                            let totalCount = evidence.mediaProbes.count
                            let stabilityText = String(stableCount) + " of " + String(totalCount) + " new files stayed the same size over three seconds."
                            let readabilityText = String(readableCount) + " were readable by macOS media services."
                            Text(stabilityText)
                                .font(.caption).foregroundStyle(.secondary)
                            Text(readabilityText)
                                .font(.caption).foregroundStyle(.secondary)
                            Text("This is an app-level evidence check, not final delivery proof. It does not yet prove the normal controls were used or that an editor can access the media.")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        if let error = evidence.errorMessage {
                            Text(error).font(.caption).foregroundStyle(.orange)
                        }
                        if let receipt = evidence.receipts.first {
                            Text("Latest persisted receipt: " + receipt.kind.rawValue + " · " + receipt.recordedAt.formatted(date: .abbreviated, time: .standard))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    .padding(4)
                }

            }
            .padding(28)
        }
    }
}

private struct FolderAccessRow: View {
    let role: RecordingFolderRole
    let access: RecordingFolderAccess?
    let check: RecordingFolderCheck
    let choose: () -> Void
    let clear: () -> Void

    var body: some View {
        GroupBox {
            HStack(alignment: .top, spacing: 14) {
                Image(systemName: "folder.fill")
                    .font(.title2)
                    .foregroundStyle(check.color)
                VStack(alignment: .leading, spacing: 6) {
                    Text(role.title).font(.headline)
                    Text(role.detail).foregroundStyle(.secondary)
                    if let access {
                        Text(access.displayPath).font(.caption.monospaced()).foregroundStyle(.secondary)
                        Text("Saved location. StudioOS will keep using it unless you change it.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Text(check.title).font(.subheadline.weight(.semibold)).foregroundStyle(check.color)
                    Text(check.detail).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 8) {
                    Button(access == nil ? "Choose folder" : "Change folder", action: choose)
                    if access != nil { Button("Forget", role: .destructive, action: clear) }
                }
                .buttonStyle(.bordered)
            }
            .padding(6)
        }
    }
}
