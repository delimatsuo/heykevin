import SwiftUI

struct ContentView: View {
    @EnvironmentObject var appState: AppState
    @ObservedObject var callManager = CallManager.shared

    var body: some View {
        TabView {
            CallHistoryView()
                .tabItem {
                    Label("Calls", systemImage: "phone.fill")
                }

            ContactsView()
                .tabItem {
                    Label("Contacts", systemImage: "person.crop.circle")
                }

            SettingsView()
                .tabItem {
                    Label("Settings", systemImage: "gear")
                }
        }
        .onAppear {
            setupCallHandlers()
        }
    }

    private func setupCallHandlers() {
        // When user accepts a call via CallKit
        callManager.onCallAccepted = { uuid in
            Task {
                let callSid = AppState.shared.pendingCallSid
                let conf = AppState.shared.pendingConferenceName

                // Tell server we're accepting
                await APIClient.shared.sendCallAction(callSid: callSid, action: "accept")

                // Get Twilio token and connect via VoIP
                if let token = await APIClient.shared.getVoIPToken(
                    callSid: callSid,
                    conferenceName: conf
                ) {
                    // TODO Phase 3: Connect via Twilio Voice SDK
                    print("Got Twilio token, connecting to conference...")
                }
            }
        }

        // When user declines a call via CallKit
        callManager.onCallDeclined = { uuid in
            Task {
                let callSid = AppState.shared.pendingCallSid
                await APIClient.shared.sendCallAction(callSid: callSid, action: "decline")
            }
        }
    }
}
