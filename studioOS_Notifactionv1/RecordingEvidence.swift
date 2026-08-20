import Combine
import Foundation

struct RecordingBaseline: Codable, Equatable {
    let capturedAt: Date
    let folders: [RecordingFolderRole: [RecordingFolderItem]]
}

struct RecordingEvidenceSummary: Codable, Equatable {
    let baselineAt: Date
    let newItems: [RecordingFolderRole: [RecordingFolderItem]]

    var totalNewItems: Int {
        newItems.values.reduce(0) { partial, items in partial + items.count }
    }
}

enum RecordingEvidenceReceiptKind: String, Codable {
    case baselineCaptured
    case postRecordComparison
    case mediaVerification
}

struct RecordingEvidenceReceipt: Codable, Equatable, Identifiable {
    let id: UUID
    let kind: RecordingEvidenceReceiptKind
    let recordedAt: Date
    let baselineAt: Date
    let newItems: [RecordingFolderRole: [RecordingFolderItem]]
    let mediaProbes: [RecordingMediaProbe]
    let note: String
}

@MainActor
final class RecordingEvidenceStore: ObservableObject {
    @Published private(set) var baseline: RecordingBaseline?
    @Published private(set) var lastSummary: RecordingEvidenceSummary?
    @Published private(set) var mediaProbes: [RecordingMediaProbe] = []
    @Published private(set) var receipts: [RecordingEvidenceReceipt] = []
    @Published private(set) var errorMessage: String?

    private let storageKey = "studioOS.recordingEvidenceBaseline.v1"
    private let receiptsStorageKey = "studioOS.recordingEvidenceReceipts.v1"

    init() {
        if let data = UserDefaults.standard.data(forKey: storageKey),
           let saved = try? JSONDecoder().decode(RecordingBaseline.self, from: data) {
            baseline = saved
        }
        if let receiptData = UserDefaults.standard.data(forKey: receiptsStorageKey),
           let savedReceipts = try? JSONDecoder().decode([RecordingEvidenceReceipt].self, from: receiptData) {
            receipts = savedReceipts
        }
    }

    func captureBaseline(using folders: RecordingFolderAccessStore) {
        do {
            var snapshots: [RecordingFolderRole: [RecordingFolderItem]] = [:]
            for role in RecordingFolderRole.allCases {
                snapshots[role] = try folders.snapshot(for: role)
            }
            let captured = RecordingBaseline(capturedAt: Date(), folders: snapshots)
            baseline = captured
            lastSummary = nil
            mediaProbes = []
            errorMessage = nil
            save(captured)
            appendReceipt(.baselineCaptured, baseline: captured, newItems: [:], probes: [], note: "Read-only baseline captured before a future take.")
        } catch {
            errorMessage = "StudioOS could not capture a baseline: " + error.localizedDescription
        }
    }

    func compareWithBaseline(using folders: RecordingFolderAccessStore) {
        guard let baseline else {
            errorMessage = "Capture a baseline before checking for new recording files."
            return
        }
        do {
            var newItems: [RecordingFolderRole: [RecordingFolderItem]] = [:]
            for role in RecordingFolderRole.allCases {
                let knownNames = Set((baseline.folders[role] ?? []).map(\.name))
                newItems[role] = try folders.snapshot(for: role).filter { !knownNames.contains($0.name) }
            }
            lastSummary = RecordingEvidenceSummary(baselineAt: baseline.capturedAt, newItems: newItems)
            mediaProbes = []
            errorMessage = nil
            appendReceipt(.postRecordComparison, baseline: baseline, newItems: newItems, probes: [], note: "Read-only comparison against the saved baseline. New file names are not delivery proof.")
        } catch {
            errorMessage = "StudioOS could not compare the recording folders: " + error.localizedDescription
        }
    }

    func verifyNewMedia(using folders: RecordingFolderAccessStore) async {
        guard let summary = lastSummary, let baseline else {
            errorMessage = "Check for new files before verifying media."
            return
        }
        do {
            var probes: [RecordingMediaProbe] = []
            for role in RecordingFolderRole.allCases {
                let items = summary.newItems[role] ?? []
                guard !items.isEmpty else { continue }
                probes.append(contentsOf: try await folders.probeMedia(items, in: role))
            }
            mediaProbes = probes
            errorMessage = nil
            appendReceipt(.mediaVerification, baseline: baseline, newItems: summary.newItems, probes: probes, note: "Read-only stability and macOS readability check. This does not prove editor delivery.")
        } catch {
            errorMessage = "StudioOS could not verify the new media: " + error.localizedDescription
        }
    }

    private func save(_ baseline: RecordingBaseline) {
        guard let data = try? JSONEncoder().encode(baseline) else { return }
        UserDefaults.standard.set(data, forKey: storageKey)
    }

    private func appendReceipt(
        _ kind: RecordingEvidenceReceiptKind,
        baseline: RecordingBaseline,
        newItems: [RecordingFolderRole: [RecordingFolderItem]],
        probes: [RecordingMediaProbe],
        note: String
    ) {
        receipts.insert(
            RecordingEvidenceReceipt(
                id: UUID(),
                kind: kind,
                recordedAt: Date(),
                baselineAt: baseline.capturedAt,
                newItems: newItems,
                mediaProbes: probes,
                note: note
            ),
            at: 0
        )
        receipts = Array(receipts.prefix(30))
        guard let data = try? JSONEncoder().encode(receipts) else { return }
        UserDefaults.standard.set(data, forKey: receiptsStorageKey)
    }
}
