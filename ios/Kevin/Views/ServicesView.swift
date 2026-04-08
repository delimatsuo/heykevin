import SwiftUI

struct ServiceItem: Identifiable {
    let id = UUID()
    var name: String
    var priceMin: Int
    var priceMax: Int
}

struct ServicesView: View {
    @EnvironmentObject var appState: AppState
    @State private var services: [ServiceItem] = []
    @State private var isLoading = false
    @State private var showAddService = false
    @State private var newName = ""
    @State private var newPriceMin = ""
    @State private var newPriceMax = ""

    var body: some View {
        List {
            Section {
                Text(String(localized: "Add your services and prices so Kevin can quote estimates to callers."))
                    .foregroundStyle(.secondary)
                    .font(.subheadline)
                    .listRowBackground(Color.clear)
            }

            Section {
                Button {
                    showAddService = true
                } label: {
                    Label(String(localized: "Add Service"), systemImage: "plus")
                }
            }

            if !services.isEmpty {
                Section(String(localized: "Services & Pricing")) {
                    ForEach(services) { service in
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(service.name)
                                    .font(.body)
                                if service.priceMin == service.priceMax {
                                    Text(String(localized: "$\(service.priceMin)"))
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                } else {
                                    Text(String(localized: "$\(service.priceMin) - $\(service.priceMax)"))
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            Spacer()
                        }
                    }
                    .onDelete(perform: deleteService)
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle(String(localized: "Services & Pricing"))
        .alert(String(localized: "Add Service"), isPresented: $showAddService) {
            TextField(String(localized: "Service name"), text: $newName)
            TextField(String(localized: "Min price"), text: $newPriceMin)
                .keyboardType(.numberPad)
            TextField(String(localized: "Max price"), text: $newPriceMax)
                .keyboardType(.numberPad)
            Button(String(localized: "Add")) {
                addService()
            }
            Button(String(localized: "Cancel"), role: .cancel) {
                clearForm()
            }
        }
        .task { await loadServices() }
        .overlay {
            if isLoading {
                ProgressView()
            }
        }
    }

    private func loadServices() async {
        isLoading = true
        let data = await APIClient.shared.getServices(contractorId: appState.contractorId)
        services = data.map { dict in
            ServiceItem(
                name: dict["name"] as? String ?? "",
                priceMin: dict["price_min"] as? Int ?? 0,
                priceMax: dict["price_max"] as? Int ?? 0
            )
        }
        isLoading = false
    }

    private func addService() {
        let name = newName.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else { return }
        let min = Int(newPriceMin) ?? 0
        let max = Int(newPriceMax) ?? min

        services.append(ServiceItem(name: name, priceMin: min, priceMax: max))
        clearForm()
        Task { await saveServices() }
    }

    private func deleteService(at offsets: IndexSet) {
        services.remove(atOffsets: offsets)
        Task { await saveServices() }
    }

    private func saveServices() async {
        let data = services.map { s -> [String: Any] in
            ["name": s.name, "price_min": s.priceMin, "price_max": s.priceMax]
        }
        _ = await APIClient.shared.updateServices(contractorId: appState.contractorId, services: data)
    }

    private func clearForm() {
        newName = ""
        newPriceMin = ""
        newPriceMax = ""
    }
}
