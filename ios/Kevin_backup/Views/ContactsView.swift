import SwiftUI

struct ContactsView: View {
    var body: some View {
        NavigationStack {
            List {
                Section("Whitelisted") {
                    Text("No contacts whitelisted yet")
                        .foregroundStyle(.secondary)
                }

                Section {
                    Button("Import iPhone Contacts") {
                        // TODO Phase 6: Import contacts
                    }
                }
            }
            .navigationTitle("Contacts")
        }
    }
}
