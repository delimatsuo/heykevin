import SwiftUI

struct CallHistoryView: View {
    @State private var calls: [CallRecord] = []
    @State private var isLoading = false

    var body: some View {
        NavigationStack {
            List {
                if calls.isEmpty && !isLoading {
                    ContentUnavailableView(
                        "No calls yet",
                        systemImage: "phone.badge.checkmark",
                        description: Text("Screened calls will appear here")
                    )
                }

                ForEach(calls) { call in
                    CallRowView(call: call)
                }
            }
            .navigationTitle("Kevin")
            .refreshable {
                await loadCalls()
            }
            .task {
                await loadCalls()
            }
        }
    }

    private func loadCalls() async {
        isLoading = true
        calls = await APIClient.shared.getCallHistory()
        isLoading = false
    }
}

struct CallRowView: View {
    let call: CallRecord

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text(call.callerName.isEmpty ? call.callerPhone : call.callerName)
                    .font(.headline)
                if !call.callerName.isEmpty {
                    Text(call.callerPhone)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Text(call.outcome.capitalized)
                    .font(.caption)
                    .foregroundStyle(outcomeColor)
            }

            Spacer()

            Text(call.timestamp, style: .time)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 4)
    }

    private var outcomeColor: Color {
        switch call.outcome {
        case "picked_up": return .green
        case "voicemail": return .blue
        case "ignored": return .orange
        case "spam", "blocked": return .red
        default: return .secondary
        }
    }
}
