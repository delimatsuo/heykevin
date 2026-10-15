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
                VStack(alignment: .leading, spacing: 6) {
                    Text(String(localized: "RECEPTIONIST MENU"))
                        .font(.system(size: 11, weight: .bold))
                        .tracking(0.8)
                        .foregroundStyle(Color.hkInkMuted)
                    Text(String(localized: "Add your services and prices so Kevin can quote estimates to callers."))
                        .foregroundStyle(Color.hkInkSecondary)
                        .font(.subheadline)
                }
                .listRowInsets(EdgeInsets(top: 8, leading: 4, bottom: 4, trailing: 4))
                .listRowBackground(Color.clear)
            }

            Section {
                Button {
                    showAddService = true
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "plus.circle.fill")
                            .foregroundStyle(Color.hkInk)
                        Text(String(localized: "Add Service"))
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(Color.hkInk)
                        Spacer()
                    }
                    .padding(.vertical, 4)
                }
            }

            if !services.isEmpty {
                Section(String(localized: "Services & Pricing")) {
                    ForEach(services) { service in
                        HStack(spacing: 12) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(service.name)
                                    .font(.subheadline.weight(.semibold))
                                    .foregroundStyle(Color.hkInk)
                                if service.priceMin <= 0 && service.priceMax <= 0 {
                                    Text(String(localized: "No price listed"))
                                        .font(.caption.weight(.medium))
                                        .foregroundStyle(Color.hkInkMuted)
                                } else if service.priceMin == service.priceMax || service.priceMax <= 0 {
                                    Text(String(localized: "$\(service.priceMin)"))
                                        .font(.caption.weight(.semibold))
                                        .foregroundStyle(Color.hkGreen)
                                } else if service.priceMin <= 0 {
                                    Text(String(localized: "Up to $\(service.priceMax)"))
                                        .font(.caption.weight(.semibold))
                                        .foregroundStyle(Color.hkGreen)
                                } else {
                                    Text(String(localized: "$\(service.priceMin) - $\(service.priceMax)"))
                                        .font(.caption.weight(.semibold))
                                        .foregroundStyle(Color.hkGreen)
                                }
                            }
                            Spacer()
                            Image(systemName: "checkmark.circle.fill")
                                .foregroundStyle(Color.hkGreen)
                        }
                        .padding(.vertical, 2)
                    }
                    .onDelete(perform: deleteService)
                }
            }
        }
        .listStyle(.insetGrouped)
        .scrollContentBackground(.hidden)
        .background(Color.hkWarmCanvas.ignoresSafeArea())
        .preferredColorScheme(.light)
        .navigationTitle(String(localized: "Services & Pricing"))
        .alert(String(localized: "Add Service"), isPresented: $showAddService) {
            TextField(String(localized: "Service name"), text: $newName)
            TextField(String(localized: "Min price (optional)"), text: $newPriceMin)
                .keyboardType(.numberPad)
            TextField(String(localized: "Max price (optional)"), text: $newPriceMax)
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
