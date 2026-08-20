import AVFoundation
import Foundation

enum MediaReadability {
    static func canRead(_ url: URL) async -> Bool {
        let asset = AVURLAsset(url: url)
        do {
            let playable = try await asset.load(.isPlayable)
            let duration = try await asset.load(.duration)
            return playable && duration.isNumeric && duration.seconds > 0
        } catch {
            return false
        }
    }
}
