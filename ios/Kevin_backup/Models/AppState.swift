import Foundation
import Combine

/// Global app state — shared across views and services.
class AppState: ObservableObject {
    static let shared = AppState()

    // Backend URL
    let backendURL = "https://kevin-api-752910912062.us-central1.run.app"

    // Device registration
    @Published var voipToken: String = ""
    @Published var isRegistered: Bool = false

    // Pending call info (set by VoIP push, used when user accepts)
    @Published var pendingCallSid: String = ""
    @Published var pendingConferenceName: String = ""

    // Call history
    @Published var calls: [CallRecord] = []

    // User settings
    @Published var userName: String = "Deli"
    @Published var apiToken: String = ""  // Bearer token for API auth
}
