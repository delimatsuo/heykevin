import SwiftUI
import UIKit

struct OnboardingForwardingStepView: View {
    @EnvironmentObject var appState: AppState
    let kevinNumber: String
    let countryCode: String
    let mode: String
    @Binding var isVerizon: Bool
    @Binding var forwardingInstructions: ForwardingInstructions?
    let onComplete: (_ didSetUp: Bool) -> Void

    init(
        kevinNumber: String,
        countryCode: String,
        mode: String = "business",
        isVerizon: Binding<Bool>,
        forwardingInstructions: Binding<ForwardingInstructions?>,
        onComplete: @escaping (_ didSetUp: Bool) -> Void
    ) {
        self.kevinNumber = kevinNumber
        self.countryCode = countryCode
        self.mode = mode
        self._isVerizon = isVerizon
        self._forwardingInstructions = forwardingInstructions
        self.onComplete = onComplete
    }

    static func resolvedDialCodes(
        kevinNumber: String,
        countryCode: String,
        isVerizon: Bool,
        instructions: ForwardingInstructions?
    ) -> ForwardingCodes? {
        ForwardingDialCodes.codes(
            countryCode: countryCode,
            instructions: instructions,
            number: kevinNumber,
            isVerizon: isVerizon
        )
    }

    var forwardingCodes: ForwardingCodes? {
        Self.resolvedDialCodes(
            kevinNumber: kevinNumber,
            countryCode: countryCode,
            isVerizon: isVerizon,
            instructions: forwardingInstructions
        )
    }

    @MainActor
    func applyCarrierSelection(isVerizon newValue: Bool, to targetAppState: AppState? = nil) {
        isVerizon = newValue
        if let targetAppState {
            targetAppState.isVerizonCarrier = newValue
        } else {
            appState.isVerizonCarrier = newValue
        }
    }

    @MainActor
    func loadForwardingInstructionsIfNeeded(
        fetch: (String) async -> ForwardingInstructions? = { country in
            await APIClient.shared.getForwardingInstructions(countryCode: country)
        }
    ) async {
        guard !ForwardingCountry.isNANP(countryCode) else { return }
        if let fetched = await fetch(countryCode) {
            forwardingInstructions = fetched
        }
    }

    func completeStep(didSetUp: Bool) {
        onComplete(didSetUp)
    }

    var body: some View {
        VStack(spacing: 20) {
            Text(String(localized: "Set Up Call Forwarding"))
                .font(.title.bold())

            Text(String(localized: "Forward your missed calls to Kevin.\nYour phone rings first — Kevin catches what you miss."))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            // Carrier choice comes FIRST: the dial buttons below derive their
            // codes from it. The existing carrier picker is available only in the US.
            Group {
                if countryCode != "US" {
                    Text(countryCode == "CA" ? CanadaForwardingGuidance.title : String(localized: "Carrier setup in preparation"))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(String(localized: "Your carrier"))
                            .font(.subheadline.weight(.medium))
                        Picker(String(localized: "Your carrier"), selection: $isVerizon) {
                            Text(String(localized: "AT&T, T-Mobile, other")).tag(false)
                            Text(String(localized: "Verizon")).tag(true)
                        }
                        .pickerStyle(.segmented)
                        .onChange(of: isVerizon) { _, newValue in
                            applyCarrierSelection(isVerizon: newValue)
                        }
                        Text(String(localized: "Verizon uses different codes — pick first, then dial."))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .task(id: countryCode) {
                await loadForwardingInstructionsIfNeeded()
            }

            if let codes = forwardingCodes {
                VStack(spacing: 12) {
                    // Step 1: Clear existing forwarding
                    Button {
                        if let url = ForwardingDialCodes.telURL(codes.clearExisting) {
                            UIApplication.shared.open(url)
                        }
                    } label: {
                        HStack {
                            Text("1")
                                .font(.caption.bold())
                                .frame(width: 24, height: 24)
                                .background(Circle().fill(.blue))
                                .foregroundStyle(.white)
                            Text(String(localized: "Clear existing forwarding"))
                                .font(.subheadline)
                            Spacer()
                            Image(systemName: "phone.arrow.right")
                        }
                        .padding()
                        .background(Color(.systemGray6))
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                    }
                    .buttonStyle(.plain)

                    // Step 2: Set Kevin forwarding
                    Button {
                        if let url = ForwardingDialCodes.telURL(codes.activate) {
                            UIApplication.shared.open(url)
                        }
                    } label: {
                        HStack {
                            Text("2")
                                .font(.caption.bold())
                                .frame(width: 24, height: 24)
                                .background(Circle().fill(.blue))
                                .foregroundStyle(.white)
                            Text(String(localized: "Forward missed calls to Kevin"))
                                .font(.subheadline)
                            Spacer()
                            Image(systemName: "phone.arrow.right")
                        }
                        .padding()
                        .background(Color(.systemGray6))
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                    }
                    .buttonStyle(.plain)
                }
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    Label(countryCode == "CA" ? CanadaForwardingGuidance.title : String(localized: "Call forwarding in preparation"), systemImage: "info.circle.fill")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(.blue)
                    Text(countryCode == "CA" ? CanadaForwardingGuidance.setup : String(localized: "Call forwarding for your region is currently being prepared. No carrier dialing codes are required at this time."))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    if countryCode == "CA" {
                        Text(CanadaForwardingGuidance.baseline)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding()
                .background(Color.blue.opacity(0.10))
                .clipShape(RoundedRectangle(cornerRadius: 12))
            }

            // iOS Live Voicemail answers calls on-device before the carrier's
            // no-answer timer fires, so the forward never triggers. Apple's own
            // guidance is to turn it off when carrier forwarding misbehaves.
            // There is no URL scheme that deep-links here — Settings can only be
            // opened to our own app's pane — so these are plain instructions.
            if countryCode != "CA" {
                VStack(alignment: .leading, spacing: 6) {
                    Label(String(localized: "Turn off Live Voicemail first"), systemImage: "exclamationmark.triangle.fill")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(.orange)
                    Text(String(localized: "If Live Voicemail is on, your iPhone answers before Kevin can. Open Settings, find Phone, then Live Voicemail, and switch it off."))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding()
                .background(Color.orange.opacity(0.10))
                .clipShape(RoundedRectangle(cornerRadius: 12))
            }

            Text(String(localized: "Your Kevin number: \(kevinNumber)"))
                .font(.subheadline.monospacedDigit())
                .foregroundStyle(.secondary)

            Spacer()

            Button {
                completeStep(didSetUp: true)
            } label: {
                Text(countryCode == "CA" ? String(localized: "Continue — set up with my carrier later") : String(localized: "I'm All Set"))
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
            }
            .buttonStyle(.borderedProminent)
            .clipShape(RoundedRectangle(cornerRadius: 14))

            Button(String(localized: "Skip for now")) {
                completeStep(didSetUp: false)
            }
            .font(.subheadline)
            .foregroundStyle(.secondary)
        }
    }
}
