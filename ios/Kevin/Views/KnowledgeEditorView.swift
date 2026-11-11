import SwiftUI
import AVFoundation
import Speech

private func debugLog(_ message: String) {
    #if DEBUG
    print(message)
    #endif
}

private enum KnowledgeVoiceMode: String, CaseIterable, Identifiable {
    case add
    case replace

    var id: String { rawValue }

    var title: String {
        switch self {
        case .add: return String(localized: "Add")
        case .replace: return String(localized: "Replace")
        }
    }
}

struct KnowledgeEditorView: View {
    @Environment(\.dismiss) var dismiss
    @EnvironmentObject var appState: AppState
    @Binding var knowledgeText: String
    @StateObject private var saveCoordinator = KnowledgeSaveCoordinator()
    @State private var editorAuth: CallAuthContext? = nil
    @State private var isRecording = false
    @State private var audioRecorder: AVAudioRecorder? = nil
    @State private var isTranscribing = false
    @State private var recordingURL: URL? = nil
    @State private var knowledgeLengthWarning = ""
    @State private var voiceMode: KnowledgeVoiceMode = .add
    @State private var pendingKnowledge = ""
    @State private var showKnowledgeDraft = false
    @State private var showClearKnowledge = false

    init(knowledgeText: Binding<String>) {
        self._knowledgeText = knowledgeText
    }

    private var isFixtureMode: Bool {
        #if DEBUG
        return AppStoreScreenshotFixtures.isEnabled
        #else
        return false
        #endif
    }

    private let placeholder = """
## Services
- Faucet repair ($150-350)
- Water heater install ($800-2500)
- Drain cleaning ($150-250)

## NOT Offered
- Commercial plumbing

## Hours
Mon-Fri 7am-6pm, Sat 8am-2pm

## Service Area
San Jose, Santa Clara, Campbell

## Pricing
- Service call fee: $89
"""

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                HStack(spacing: 12) {
                    Button {
                        if isRecording {
                            stopRecording()
                        } else {
                            startRecording()
                        }
                    } label: {
                        HStack(spacing: 10) {
                            Image(systemName: isRecording ? "stop.circle.fill" : "mic.circle.fill")
                                .font(.title2)
                                .foregroundStyle(isRecording ? Color.hkRed : Color.hkInk)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(isRecording ? String(localized: "Tap to stop") : String(localized: "Describe your business"))
                                    .font(.subheadline.weight(.semibold))
                                    .foregroundStyle(Color.hkInk)
                                Text(isRecording ? String(localized: "Recording...") : String(localized: "Talk and Kevin will learn"))
                                    .font(.caption)
                                    .foregroundStyle(Color.hkInkSecondary)
                            }
                        }
                    }
                    .buttonStyle(.plain)
                    .disabled(isFixtureMode)

                    Spacer()

                    if isTranscribing {
                        ProgressView()
                            .scaleEffect(0.8)
                    }
                }
                .padding(14)
                .background(
                    RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .fill(Color.white)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .strokeBorder(Color.hkDivider, lineWidth: 1)
                )
                .padding(.horizontal)
                .padding(.top, 8)

                Picker(String(localized: "Voice Update Mode"), selection: $voiceMode) {
                    ForEach(KnowledgeVoiceMode.allCases) { mode in
                        Text(mode.title).tag(mode)
                    }
                }
                .pickerStyle(.segmented)
                .padding(.horizontal)
                .padding(.top, 8)
                .disabled(isFixtureMode)

                ZStack(alignment: .topLeading) {
                    TextEditor(text: $knowledgeText)
                        .font(.system(.subheadline, design: .monospaced))
                        .foregroundStyle(Color.hkInk)
                        .scrollContentBackground(.hidden)

                    if knowledgeText.isEmpty {
                        Text(placeholder)
                            .font(.system(.subheadline, design: .monospaced))
                            .foregroundStyle(Color.hkInkMuted)
                            .padding(.top, 8)
                            .padding(.leading, 5)
                            .allowsHitTesting(false)
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(
                    RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .fill(Color.white)
                        .padding(.horizontal, 8)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .strokeBorder(Color.hkDivider, lineWidth: 1)
                        .padding(.horizontal, 8)
                )
                .padding(.top, 8)

                if let errorMessage = saveCoordinator.errorMessage {
                    Text(errorMessage)
                        .font(.callout)
                        .foregroundStyle(Color.hkRed)
                        .padding(.horizontal)
                        .accessibilityIdentifier("knowledgeSaveError")
                }

                if !knowledgeLengthWarning.isEmpty {
                    Text(knowledgeLengthWarning)
                        .font(.caption)
                        .foregroundStyle(Color.hkOrange)
                        .padding(.horizontal)
                        .padding(.top, 4)
                }

                Text(String(localized: "Type your services, or tap the mic to describe them by voice. Kevin uses this to answer caller questions."))
                    .font(.caption)
                    .foregroundStyle(Color.hkInkMuted)
                    .padding(.horizontal)
                    .padding(.top, 4)
                    .padding(.bottom, 8)

                if !knowledgeText.isEmpty {
                    Button(role: .destructive) {
                        showClearKnowledge = true
                    } label: {
                        Label(String(localized: "Clear Business Knowledge"), systemImage: "trash")
                    }
                    .font(.subheadline)
                    .padding(.bottom, 12)
                    .disabled(isFixtureMode)
                }
            }
            .disabled(saveCoordinator.isSaving)
            .background(Color.hkWarmCanvas.ignoresSafeArea())
            .onAppear {
                if editorAuth == nil { editorAuth = appState.currentAuthContext() }
            }
            .onDisappear { saveCoordinator.invalidate() }
            .onChange(of: appState.currentAuthContext()) { _, _ in
                saveCoordinator.invalidate()
            }
            .interactiveDismissDisabled(saveCoordinator.isSaving)
            .preferredColorScheme(.light)
            .onChange(of: knowledgeText) { _, newValue in
                if newValue.count > 10_000 {
                    knowledgeText = String(newValue.prefix(10_000))
                    knowledgeLengthWarning = String(localized: "Knowledge text truncated to 10,000 characters.")
                } else {
                    knowledgeLengthWarning = ""
                }
            }
            .navigationTitle(String(localized: "Business Knowledge"))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button(String(localized: "Cancel")) {
                        saveCoordinator.invalidate()
                        dismiss()
                    }
                    .foregroundStyle(Color.hkInk)
                    .disabled(saveCoordinator.isSaving)
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button(String(localized: "Save")) {
                        Task { await saveKnowledge() }
                    }
                    .fontWeight(.semibold)
                    .foregroundStyle(Color.hkGreen)
                    .disabled(saveCoordinator.isSaving || isRecording || isTranscribing || isFixtureMode)
                }
            }
            .confirmationDialog(
                String(localized: "Clear all business knowledge?"),
                isPresented: $showClearKnowledge,
                titleVisibility: .visible
            ) {
                Button(String(localized: "Clear All"), role: .destructive) {
                    knowledgeText = ""
                }
                Button(String(localized: "Cancel"), role: .cancel) {}
            } message: {
                Text(String(localized: "This clears the knowledge Kevin uses to answer business questions. It is not saved until you tap Save."))
            }
            .sheet(isPresented: $showKnowledgeDraft) {
                NavigationStack {
                    TextEditor(text: $pendingKnowledge)
                        .font(.system(.subheadline, design: .monospaced))
                        .foregroundStyle(Color.hkInk)
                        .scrollContentBackground(.hidden)
                        .padding()
                        .background(Color.hkWarmCanvas.ignoresSafeArea())
                        .preferredColorScheme(.light)
                        .navigationTitle(String(localized: "Review Changes"))
                        .navigationBarTitleDisplayMode(.inline)
                        .toolbar {
                            ToolbarItem(placement: .topBarLeading) {
                                Button(String(localized: "Discard")) {
                                    showKnowledgeDraft = false
                                }
                            }
                            ToolbarItem(placement: .topBarTrailing) {
                                Button(String(localized: "Apply")) {
                                    knowledgeText = pendingKnowledge
                                    showKnowledgeDraft = false
                                }
                                .fontWeight(.semibold)
                                .disabled(pendingKnowledge.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                            }
                        }
                }
            }
        }
    }

    private func startRecording() {
        #if DEBUG
        if isFixtureMode { return }
        #endif
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(.record, mode: .default)
            try session.setActive(true)
        } catch {
            debugLog("Audio session error: \(error)")
            return
        }

        let url = FileManager.default.temporaryDirectory.appendingPathComponent("kevin_training.m4a")
        recordingURL = url

        let recSettings: [String: Any] = [
            AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
            AVSampleRateKey: 16000,
            AVNumberOfChannelsKey: 1,
            AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
        ]

        do {
            audioRecorder = try AVAudioRecorder(url: url, settings: recSettings)
            audioRecorder?.record()
            isRecording = true
        } catch {
            debugLog("Recording error: \(error)")
        }
    }

    private func stopRecording() {
        audioRecorder?.stop()
        isRecording = false

        guard let url = recordingURL else { return }
        isTranscribing = true

        Task {
            let transcript = await transcribeLocally(url: url)
            if let transcript = transcript, !transcript.isEmpty {
                if let knowledge = await APIClient.shared.structureKnowledge(
                    contractorId: appState.contractorId,
                    rawText: transcript,
                    existingKnowledge: knowledgeText,
                    mode: voiceMode.rawValue
                ) {
                    await MainActor.run {
                        let trimmed = knowledge.trimmingCharacters(in: .whitespacesAndNewlines)
                        if trimmed.isEmpty {
                            knowledgeLengthWarning = String(localized: "No business details were detected in that recording.")
                        } else if voiceMode == .replace || knowledgeText.isEmpty {
                            pendingKnowledge = trimmed
                            showKnowledgeDraft = true
                        } else {
                            pendingKnowledge = knowledgeText + "\n\n" + trimmed
                            showKnowledgeDraft = true
                        }
                    }
                }
            }
            try? FileManager.default.removeItem(at: url)
            await MainActor.run { isTranscribing = false }
        }
    }

    private func transcribeLocally(url: URL) async -> String? {
        let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
        guard let recognizer = recognizer, recognizer.isAvailable else { return nil }

        let authStatus = await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status)
            }
        }
        guard authStatus == .authorized else { return nil }

        let request = SFSpeechURLRecognitionRequest(url: url)
        request.shouldReportPartialResults = false

        return await withCheckedContinuation { continuation in
            recognizer.recognitionTask(with: request) { result, error in
                if let result = result, result.isFinal {
                    continuation.resume(returning: result.bestTranscription.formattedString)
                } else if error != nil {
                    continuation.resume(returning: nil)
                }
            }
        }
    }

    private func saveKnowledge() async {
        guard let auth = editorAuth, !isRecording, !isTranscribing, !isFixtureMode else { return }
        let saved = await saveCoordinator.save(
            knowledge: knowledgeText,
            auth: auth,
            currentAuth: { appState.currentAuthContext() },
            currentKnowledge: { knowledgeText },
            persist: { knowledge, auth in
                try await APIClient.shared.updateKnowledge(knowledge: knowledge, auth: auth)
            }
        )
        if saved { dismiss() }
    }
}
