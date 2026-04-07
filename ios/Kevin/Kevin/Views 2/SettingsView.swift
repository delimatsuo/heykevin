import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var appState: AppState
    @ObservedObject var callManager = CallManager.shared

    var body: some View {
        NavigationStack {
            List {
                Section("Status") {
                    HStack {
                        Text("VoIP Push")
                        Spacer()
                        Image(systemName: appState.isRegistered ? "checkmark.circle.fill" : "xmark.circle")
                            .foregroundStyle(appState.isRegistered ? .green : .red)
                    }

                    HStack {
                        Text("Backend")
                        Spacer()
                        Image(systemName: "checkmark.circle.fill")
                            .foregroundStyle(.green)
                    }
                }

                Section("Kevin") {
                    HStack {
                        Text("Greeting Name")
                        Spacer()
                        Text(appState.userName)
                            .foregroundStyle(.secondary)
                    }
                }

                Section("Call Forwarding") {
                    Text("To activate Kevin, forward your calls:")
                        .font(.caption)
                        .foregroundStyle(.secondary)

                    VStack(alignment: .leading, spacing: 8) {
                        Text("AT&T / T-Mobile:")
                            .font(.caption.bold())
                        Text("Dial: *21*16504222677#")
                            .font(.system(.caption, design: .monospaced))

                        Text("Verizon:")
                            .font(.caption.bold())
                        Text("Dial: *72 16504222677")
                            .font(.system(.caption, design: .monospaced))

                        Text("To disable:")
                            .font(.caption.bold())
                        Text("AT&T/T-Mobile: ##21#\nVerizon: *73")
                            .font(.system(.caption, design: .monospaced))
                    }
                }
            }
            .navigationTitle("Settings")
        }
    }
}
