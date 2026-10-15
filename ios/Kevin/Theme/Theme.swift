import SwiftUI

// MARK: - Color tokens

extension Color {
    /// Forest Sage accent used for live status and subtle highlights.
    static let hkBlue = Color(red: 0.176, green: 0.353, blue: 0.263)

    /// Deep Charcoal Ink for interactive controls and links.
    static let hkCobalt = Color(red: 0.098, green: 0.090, blue: 0.086)

    /// Forest Sage for primary call pickup and live indicators.
    static let hkGreen = Color(red: 0.176, green: 0.353, blue: 0.263)

    /// Forest Sage for primary call pickup.
    static let hkForest = Color(red: 0.176, green: 0.353, blue: 0.263)

    /// Warm Terracotta for trial expiry and taking-a-message status.
    static let hkOrange = Color(red: 0.722, green: 0.314, blue: 0.196)

    /// Warm Crimson for errors and destructive actions.
    static let hkRed = Color(red: 0.722, green: 0.235, blue: 0.196)

    /// Warm Stone Canvas (#FAF7F2).
    static let hkCanvas = Color(red: 0.980, green: 0.969, blue: 0.949)

    /// Warm Stone Canvas (#FAF7F2) for journal and all app screens.
    static let hkWarmCanvas = Color(red: 0.980, green: 0.969, blue: 0.949)

    /// Charcoal Ink Text (#191716).
    static let hkInk = Color(red: 0.098, green: 0.090, blue: 0.086)

    /// Warm Stone Secondary Text (#57534E).
    static let hkInkSecondary = Color(red: 0.341, green: 0.325, blue: 0.306)

    /// Muted Stone Text (#8A847D).
    static let hkInkMuted = Color(red: 0.541, green: 0.518, blue: 0.490)

    /// Fine Hairline Divider (rgba(25, 23, 22, 0.08)).
    static let hkDivider = Color(red: 0.098, green: 0.090, blue: 0.086).opacity(0.08)

    /// Soft Oat Surface (#F2ECE4) for pills, avatars, and secondary containers.
    static let hkSurface = Color(red: 0.949, green: 0.925, blue: 0.894)

    /// Deeper Sand Surface (#E6DFD5) for selected tags and borders.
    static let hkSandDeep = Color(red: 0.902, green: 0.875, blue: 0.835)

    /// Sage Tint Surface (#E9F0EC).
    static let hkSageTint = Color(red: 0.914, green: 0.941, blue: 0.925)

    /// Terracotta Tint Surface (#F9ECE8).
    static let hkAmberTint = Color(red: 0.976, green: 0.925, blue: 0.910)

    /// Subtle Card Border.
    static let hkBorder = Color(red: 0.098, green: 0.090, blue: 0.086).opacity(0.10)

    /// Pure White Card Surface (#FFFFFF).
    static let hkCallSurface = Color.white
}

/// Bridges the Color tokens above to `ShapeStyle` so they can be used directly
/// with `.foregroundStyle(.hkBlue)` / `.tint(.hkGreen)` shorthand.
extension ShapeStyle where Self == Color {
    static var hkBlue:         Color { .hkBlue }
    static var hkCobalt:       Color { .hkCobalt }
    static var hkGreen:        Color { .hkGreen }
    static var hkForest:       Color { .hkForest }
    static var hkOrange:       Color { .hkOrange }
    static var hkRed:          Color { .hkRed }
    static var hkCanvas:       Color { .hkCanvas }
    static var hkWarmCanvas:   Color { .hkWarmCanvas }
    static var hkInk:          Color { .hkInk }
    static var hkInkSecondary: Color { .hkInkSecondary }
    static var hkInkMuted:     Color { .hkInkMuted }
    static var hkDivider:      Color { .hkDivider }
    static var hkSurface:      Color { .hkSurface }
    static var hkSandDeep:     Color { .hkSandDeep }
    static var hkSageTint:     Color { .hkSageTint }
    static var hkAmberTint:    Color { .hkAmberTint }
    static var hkBorder:       Color { .hkBorder }
    static var hkCallSurface:  Color { .hkCallSurface }
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
    static let card: CGFloat = 18
    static let button: CGFloat = 999
    static let bubble: CGFloat = 18
}

// MARK: - Button styles

/// Primary CTA: full-width pill. Pick Up, Subscribe.
struct HKPrimaryButtonStyle: ButtonStyle {
    var tint: Color = .hkGreen

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 16, weight: .semibold))
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity, minHeight: 54)
            .background(tint.opacity(configuration.isPressed ? 0.85 : 1))
            .clipShape(Capsule())
            .scaleEffect(configuration.isPressed ? 0.985 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

/// Bordered secondary action: Text Reply, Restore Purchases.
struct HKSecondaryButtonStyle: ButtonStyle {
    var tint: Color = .hkInk

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 15, weight: .semibold))
            .foregroundStyle(tint)
            .frame(maxWidth: .infinity, minHeight: 48)
            .background(
                Capsule()
                    .fill(Color.hkSurface.opacity(configuration.isPressed ? 0.75 : 1.0))
            )
            .overlay(
                Capsule()
                    .stroke(Color.hkDivider, lineWidth: 1)
            )
            .scaleEffect(configuration.isPressed ? 0.985 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

/// Bordered destructive action: Ignore, cancel paths.
struct HKDestructiveButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 15, weight: .semibold))
            .foregroundStyle(.hkRed)
            .frame(maxWidth: .infinity, minHeight: 48)
            .background(
                Capsule()
                    .fill(Color.hkRed.opacity(configuration.isPressed ? 0.12 : 0.06))
            )
            .overlay(
                Capsule()
                    .stroke(Color.hkRed.opacity(0.25), lineWidth: 1)
            )
            .scaleEffect(configuration.isPressed ? 0.985 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

/// Charcoal pill primary CTA matching Hims editorial style.
struct HKDarkPrimaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 16, weight: .semibold))
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity, minHeight: 54)
            .background(Color.hkInk.opacity(configuration.isPressed ? 0.85 : 1))
            .clipShape(Capsule())
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

/// Warm editorial palette for caller avatars.
enum HKAvatarPalette {
    static func color(for key: String) -> Color {
        return .hkSurface
    }
}

/// Caller avatar circle in warm oat (#F2ECE4) with charcoal ink initials (#191716).
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

    var body: some View {
        ZStack {
            Circle()
                .fill(Color.hkSurface)
            Circle()
                .strokeBorder(Color.hkDivider, lineWidth: 1)
            if hasName {
                Text(initials)
                    .font(.system(size: size * 0.35, weight: .bold))
                    .foregroundStyle(Color.hkInk)
            } else {
                Image(systemName: "person.fill")
                    .font(.system(size: size * 0.44, weight: .semibold))
                    .foregroundStyle(Color.hkInkSecondary)
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
                .fill(Color.hkInk)
                .overlay(
                    RoundedRectangle(cornerRadius: size * 0.30, style: .continuous)
                        .stroke(Color.white.opacity(0.14), lineWidth: 1)
                )

            Image(systemName: "phone.badge.waveform.fill")
                .font(.system(size: size * 0.42, weight: .semibold))
                .foregroundStyle(Color.hkWarmCanvas)
        }
        .frame(width: size, height: size)
        .shadow(color: Color.black.opacity(0.10), radius: size * 0.16, x: 0, y: size * 0.08)
    }
}
