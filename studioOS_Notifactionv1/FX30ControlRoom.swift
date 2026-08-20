import SwiftUI
import WebKit

/// Hosts the existing FX30 bridge dashboard without duplicating its controls
/// or safety rules in StudioOS. The bridge remains the single source of truth.
struct FX30ControlRoomView: View {
    private let dashboardURL = URL(string: "http://127.0.0.1:3030/")!
    @State private var reloadID = UUID()

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("FX30 Control Room")
                        .font(.headline)
                    Text("Live FX30 bridge dashboard · localhost:3030")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("Reload", systemImage: "arrow.clockwise") {
                    reloadID = UUID()
                }
                Button("Open in Browser", systemImage: "safari") {
                    NSWorkspace.shared.open(dashboardURL)
                }
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 10)
            .background(.bar)

            Divider()

            FX30DashboardWebView(url: dashboardURL)
                .id(reloadID)
        }
        .navigationTitle("FX30 Control Room")
    }
}

private struct FX30DashboardWebView: NSViewRepresentable {
    let url: URL

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()

        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.setValue(false, forKey: "drawsBackground")
        webView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData))
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {}
}
