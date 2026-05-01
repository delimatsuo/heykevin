import SwiftUI

// MARK: - Color tokens

extension Color {
    /// Trade Signal Blue. Kevin's voice and identity. Maps to systemBlue so it
    /// brightens automatically in dark mode for legibility.
    static let hkBlue = Color(uiColor: .systemBlue)

    /// Clear Line Green. The go signal. Pick Up CTA, Live indicator.
    static let hkGreen = Color(uiColor: .systemGreen)

    /// Warning Orange. Trial expiry, taking-a-message status.
    static let hkOrange = Color(uiColor: .systemOrange)

    /// Error Red. Errors, destructive labels (Ignore tint).
    static let hkRed = Color(uiColor: .systemRed)

    /// Canvas Grey. systemGroupedBackground equivalent.
    static let hkCanvas = Color(.systemGroupedBackground)

    /// Surface Grey. Secondary surfaces, caller chat bubbles.
    static let hkSurface = Color(.systemGray5)

    /// Mid Grey. Dividers, disabled borders.
    static let hkBorder = Color(.systemGray4)
}

/// Bridges the Color tokens above to `ShapeStyle` so they can be used directly
/// with `.foregroundStyle(.hkBlue)` / `.tint(.hkGreen)` shorthand.
extension ShapeStyle where Self == Color {
    static var hkBlue:    Color { .hkBlue }
    static var hkGreen:   Color { .hkGreen }
    static var hkOrange:  Color { .hkOrange }
    static var hkRed:     Color { .hkRed }
    static var hkCanvas:  Color { .hkCanvas }
    static var hkSurface: Color { .hkSurface }
    static var hkBorder:  Color { .hkBorder }
}

// MARK: - Spacing scale (4pt base)

enum HKSpace {
    static let xs: CGFloat = 4
    static let sm: CGFloat = 8
    static let md: CGFloat = 12
    static let lg: CGFloat = 16
    static let xl: CGFloat = 20
    static let xxl: CGFloat = 24
    static let xxxl: CGFloat = 32
}

// MARK: - Corner radius scale

enum HKRadius {
    static let card: CGFloat = 12
    static let button: CGFloat = 14
    static let bubble: CGFloat = 18
}

// MARK: - Button styles

/// Primary CTA: full-width, deeply colored. Pick Up, Subscribe.
/// Pass `tint` (defaults to hkGreen) so the destructive variant can use red, dark CTA can use black, etc.
struct HKPrimaryButtonStyle: ButtonStyle {
    var tint: Color = .hkGreen

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 17, weight: .semibold))
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity, minHeight: 56)
            .background(tint.opacity(configuration.isPressed ? 0.85 : 1))
            .clipShape(RoundedRectangle(cornerRadius: HKRadius.button, style: .continuous))
            .scaleEffect(configuration.isPressed ? 0.985 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

/// Bordered secondary action: Text Reply, Restore Purchases.
struct HKSecondaryButtonStyle: ButtonStyle {
    var tint: Color = .hkBlue

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 15, weight: .regular))
            .foregroundStyle(tint)
            .frame(maxWidth: .infinity, minHeight: 48)
            .background(
                RoundedRectangle(cornerRadius: HKRadius.card, style: .continuous)
                    .fill(tint.opacity(configuration.isPressed ? 0.10 : 0.0))
            )
            .overlay(
                RoundedRectangle(cornerRadius: HKRadius.card, style: .continuous)
                    .stroke(tint.opacity(0.35), lineWidth: 1)
            )
            .scaleEffect(configuration.isPressed ? 0.985 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

/// Bordered destructive action: Ignore, cancel paths.
struct HKDestructiveButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 15, weight: .regular))
            .foregroundStyle(.hkRed)
            .frame(maxWidth: .infinity, minHeight: 48)
            .background(
                RoundedRectangle(cornerRadius: HKRadius.card, style: .continuous)
                    .fill(Color.hkRed.opacity(configuration.isPressed ? 0.10 : 0.0))
            )
            .overlay(
                RoundedRectangle(cornerRadius: HKRadius.card, style: .continuous)
                    .stroke(Color.hkRed.opacity(0.35), lineWidth: 1)
            )
            .scaleEffect(configuration.isPressed ? 0.985 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

/// Dark primary used for less-emphasized commit actions (Continue, Subscribe-via-paywall).
/// Background adapts: black in light mode, white in dark mode. Label uses the
/// inverse so contrast is preserved in both schemes.
struct HKDarkPrimaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 16, weight: .semibold))
            .foregroundStyle(Color(uiColor: .systemBackground))
            .frame(maxWidth: .infinity, minHeight: 52)
            .background(Color.primary.opacity(configuration.isPressed ? 0.85 : 1))
            .clipShape(RoundedRectangle(cornerRadius: HKRadius.button, style: .continuous))
            .scaleEffect(configuration.isPressed ? 0.985 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

// MARK: - Reusable bits

/// Pulsing dot used in "Live" / "On call" indicators.
struct HKPulseDot: View {
    var color: Color = .hkGreen
    var size: CGFloat = 8
    @State private var animate = false

    var body: some View {
        ZStack {
            Circle()
                .fill(color.opacity(0.4))
                .frame(width: size, height: size)
                .scaleEffect(animate ? 2.0 : 0.6)
                .opacity(animate ? 0.0 : 0.6)
            Circle()
                .fill(color)
                .frame(width: size, height: size)
        }
        .frame(width: size * 2, height: size * 2)
        .onAppear {
            guard !UIAccessibility.isReduceMotionEnabled else { return }
            withAnimation(.easeOut(duration: 1.6).repeatForever(autoreverses: false)) {
                animate = true
            }
        }
    }
}

/// Solid status dot, paired with a label.
struct HKStatusDot: View {
    var color: Color
    var size: CGFloat = 7
    var body: some View {
        Circle().fill(color).frame(width: size, height: size)
    }
}

/// Hashable color generator for caller avatars. Stable per phone number.
enum HKAvatarPalette {
    private static let palette: [Color] = [
        .hkBlue,
        Color(red: 0.40, green: 0.30, blue: 0.85), // purple
        .hkGreen,
        .hkOrange,
        Color(red: 0.20, green: 0.55, blue: 0.95), // teal-ish blue
        Color(red: 0.85, green: 0.30, blue: 0.55), // pink
    ]

    static func color(for key: String) -> Color {
        guard !key.isEmpty else { return .gray }
        let hash = key.unicodeScalars.reduce(0) { $0 &+ Int($1.value) }
        return palette[abs(hash) % palette.count]
    }
}

/// Caller avatar circle. Renders initials when name is present, otherwise a generic person icon.
struct HKAvatar: View {
    var name: String
    var phone: String
    var size: CGFloat = 40

    private var initials: String {
        let parts = name.split(separator: " ").prefix(2)
        let letters = parts.compactMap { $0.first }
        return String(letters).uppercased()
    }

    private var hasName: Bool { !initials.isEmpty }

    private var gradient: LinearGradient {
        if hasName {
            let base = HKAvatarPalette.color(for: phone.isEmpty ? name : phone)
            return LinearGradient(
                colors: [base.opacity(0.85), base],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
        } else {
            return LinearGradient(
                colors: [Color(.systemGray3), Color(.systemGray)],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
        }
    }

    var body: some View {
        ZStack {
            Circle().fill(gradient)
            if hasName {
                Text(initials)
                    .font(.system(size: size * 0.36, weight: .semibold))
                    .foregroundStyle(.white)
            } else {
                Image(systemName: "person.fill")
                    .font(.system(size: size * 0.5))
                    .foregroundStyle(.white)
            }
        }
        .frame(width: size, height: size)
    }
}

/// Animated three-bar indicator. Used in chat for "Kevin is responding".
struct HKListeningBars: View {
    var color: Color = .hkBlue
    @State private var animate = false

    var body: some View {
        HStack(spacing: 2) {
            ForEach(0..<3, id: \.self) { i in
                Capsule()
                    .fill(color)
                    .frame(width: 3, height: 12)
                    .scaleEffect(y: animate ? 1.0 : 0.5, anchor: .center)
                    .animation(
                        UIAccessibility.isReduceMotionEnabled
                            ? .default
                            : .easeOut(duration: 0.6)
                                .repeatForever(autoreverses: true)
                                .delay(Double(i) * 0.12),
                        value: animate
                    )
            }
        }
        .frame(height: 12)
        .onAppear { animate = true }
    }
}

/// Phone-number pretty formatter used by both Live and Recents.
enum HKPhoneFormat {
    static func display(_ raw: String) -> String {
        // Reuse existing PhoneFormatter if present, otherwise a basic fallback.
        return PhoneFormatter.format(raw)
    }
}

/// Onboarding progress: N capsule segments, the first `current` filled.
struct HKProgressBar: View {
    var current: Int
    var total: Int

    var body: some View {
        HStack(spacing: 6) {
            ForEach(0..<max(total, 1), id: \.self) { idx in
                Capsule()
                    .fill(idx < current ? Color.primary : Color(.systemGray5))
                    .frame(height: 3)
                    .frame(maxWidth: .infinity)
            }
        }
        .accessibilityElement()
        .accessibilityLabel(Text("Step \(current) of \(total)"))
    }
}

/// Simple flow layout that wraps children onto multiple lines (for tag pills).
struct FlowLayout: Layout {
    var spacing: CGFloat = 6
    var lineSpacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var x: CGFloat = 0
        var y: CGFloat = 0
        var lineHeight: CGFloat = 0
        var maxLineWidth: CGFloat = 0

        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x + size.width > maxWidth, x > 0 {
                maxLineWidth = max(maxLineWidth, x - spacing)
                x = 0
                y += lineHeight + lineSpacing
                lineHeight = 0
            }
            x += size.width + spacing
            lineHeight = max(lineHeight, size.height)
        }
        maxLineWidth = max(maxLineWidth, x - spacing)
        return CGSize(width: max(0, maxLineWidth), height: y + lineHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x: CGFloat = bounds.minX
        var y: CGFloat = bounds.minY
        var lineHeight: CGFloat = 0
        let maxX = bounds.maxX

        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x + size.width > maxX, x > bounds.minX {
                x = bounds.minX
                y += lineHeight + lineSpacing
                lineHeight = 0
            }
            subview.place(at: CGPoint(x: x, y: y), anchor: .topLeading, proposal: ProposedViewSize(size))
            x += size.width + spacing
            lineHeight = max(lineHeight, size.height)
        }
    }
}

/// Kevin brand mark. Soft-rounded square with the phone glyph; used in onboarding,
/// empty states, and any place that needs to anchor the Hey Kevin identity.
struct KevinMark: View {
    var size: CGFloat = 96

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: size * 0.30, style: .continuous)
                .fill(
                    LinearGradient(
                        colors: [
                            Color(red: 0.30, green: 0.55, blue: 1.0),
                            Color(red: 0.0, green: 0.30, blue: 0.85)
                        ],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )
                )
                .overlay(
                    RoundedRectangle(cornerRadius: size * 0.30, style: .continuous)
                        .stroke(Color.white.opacity(0.18), lineWidth: 1)
                        .blendMode(.plusLighter)
                )

            Image(systemName: "phone.fill")
                .font(.system(size: size * 0.46, weight: .regular))
                .foregroundStyle(.white)
        }
        .frame(width: size, height: size)
        .shadow(color: Color.hkBlue.opacity(0.30), radius: size * 0.18, x: 0, y: size * 0.10)
    }
}
