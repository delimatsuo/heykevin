import SwiftUI
import Contacts
import ContactsUI

struct ContactsView: View {
    @State private var vipContacts: [VIPContact] = []
    @State private var isLoading = false
    @State private var showContactPicker = false
    @State private var showAddManual = false
    @State private var manualName = ""
    @State private var manualPhone = ""

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text(String(localized: "Contacts added here will ring through directly without screening."))
                        .foregroundStyle(.secondary)
                        .font(.subheadline)
                        .listRowBackground(Color.clear)
                }

                Section {
                    Button {
                        showContactPicker = true
                    } label: {
                        Label(String(localized: "Import from iPhone"), systemImage: "square.and.arrow.down")
                    }

                    Button {
                        showAddManual = true
                    } label: {
                        Label(String(localized: "Add Number Manually"), systemImage: "plus")
                    }
                }

                if !vipContacts.isEmpty {
                    Section(String(localized: "VIP — Always Ring Through")) {
                        ForEach(vipContacts) { contact in
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(contact.name)
                                        .font(.body)
                                    Text(contact.phone)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                Image(systemName: "checkmark.circle.fill")
                                    .foregroundStyle(.green)
                            }
                        }
                        .onDelete(perform: deleteContacts)
                    }
                }
            }
            .listStyle(.insetGrouped)
            .navigationTitle(String(localized: "Contacts"))
            .sheet(isPresented: $showContactPicker) {
                ContactPickerView { name, phone in
                    Task { await addContact(name: name, phone: phone) }
                }
            }
            .alert(String(localized: "Add Number"), isPresented: $showAddManual) {
                TextField(String(localized: "Name"), text: $manualName)
                TextField(String(localized: "Phone Number"), text: $manualPhone)
                    .keyboardType(.phonePad)
                Button(String(localized: "Add")) {
                    let name = manualName
                    let phone = manualPhone
                    manualName = ""
                    manualPhone = ""
                    Task { await addContact(name: name, phone: phone) }
                }
                Button(String(localized: "Cancel"), role: .cancel) {
                    manualName = ""
                    manualPhone = ""
                }
            }
            .task { await loadContacts() }
        }
    }

    private func loadContacts() async {
        isLoading = true
        vipContacts = await APIClient.shared.getContacts()
        isLoading = false
    }

    private func addContact(name: String, phone: String) async {
        guard !phone.isEmpty else { return }
        await APIClient.shared.addContact(name: name, phone: phone)
        await loadContacts()
    }

    private func deleteContacts(at offsets: IndexSet) {
        // Remove locally for now (backend delete not critical for MVP)
        vipContacts.remove(atOffsets: offsets)
    }
}

// MARK: - VIP Contact Model

struct VIPContact: Identifiable {
    let id: String
    let name: String
    let phone: String
}

// MARK: - Contact Picker (wraps CNContactPickerViewController)

struct ContactPickerView: UIViewControllerRepresentable {
    let onSelect: (String, String) -> Void

    func makeUIViewController(context: Context) -> CNContactPickerViewController {
        let picker = CNContactPickerViewController()
        picker.delegate = context.coordinator
        picker.predicateForEnablingContact = NSPredicate(format: "phoneNumbers.@count > 0")
        return picker
    }

    func updateUIViewController(_ uiViewController: CNContactPickerViewController, context: Context) {}

    func makeCoordinator() -> Coordinator {
        Coordinator(onSelect: onSelect)
    }

    class Coordinator: NSObject, CNContactPickerDelegate {
        let onSelect: (String, String) -> Void

        init(onSelect: @escaping (String, String) -> Void) {
            self.onSelect = onSelect
        }

        func contactPicker(_ picker: CNContactPickerViewController, didSelect contact: CNContact) {
            let name = "\(contact.givenName) \(contact.familyName)".trimmingCharacters(in: .whitespaces)
            if let phone = contact.phoneNumbers.first?.value.stringValue {
                onSelect(name, phone)
            }
        }
    }
}
