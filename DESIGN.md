---
name: Hey Kevin
description: AI call screener for trades workers — instant signal, zero noise.
colors:
  trade-signal-blue: "#007AFF"
  trade-signal-blue-tint: "#007AFF1F"
  clear-line-green: "#34C759"
  clear-line-green-tint: "#34C75919"
  warning-orange: "#FF9500"
  warning-orange-tint: "#FF95001F"
  error-red: "#FF3B30"
  canvas-grey: "#F2F2F7"
  surface-grey: "#E5E5EA"
  mid-grey: "#C7C7CC"
  text-primary: "#1C1C1E"
  text-secondary: "#8E8E93"
typography:
  title2:
    fontFamily: "SF Pro Display, system-ui, -apple-system, sans-serif"
    fontSize: "22px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.3px"
  title3:
    fontFamily: "SF Pro Display, system-ui, -apple-system, sans-serif"
    fontSize: "20px"
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "-0.2px"
  headline:
    fontFamily: "SF Pro Text, system-ui, -apple-system, sans-serif"
    fontSize: "17px"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "-0.1px"
  body:
    fontFamily: "SF Pro Text, system-ui, -apple-system, sans-serif"
    fontSize: "17px"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "normal"
  subheadline:
    fontFamily: "SF Pro Text, system-ui, -apple-system, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "normal"
  caption:
    fontFamily: "SF Pro Text, system-ui, -apple-system, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.3
    letterSpacing: "0.3px"
  label-mono:
    fontFamily: "SF Pro Text, system-ui, -apple-system, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.0
    fontFeature: "\"tnum\" 1"
rounded:
  bubble: "18px"
  button-primary: "14px"
  card: "12px"
spacing:
  xs: "6px"
  sm: "8px"
  md: "16px"
  lg: "24px"
  xl: "40px"
components:
  button-cta:
    backgroundColor: "{colors.clear-line-green}"
    textColor: "#FFFFFF"
    rounded: "{rounded.button-primary}"
    padding: "14px 20px"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.trade-signal-blue}"
    rounded: "{rounded.card}"
    padding: "12px 16px"
  button-destructive:
    backgroundColor: "transparent"
    textColor: "{colors.error-red}"
    rounded: "{rounded.card}"
    padding: "12px 16px"
  chat-bubble-kevin:
    backgroundColor: "{colors.trade-signal-blue-tint}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.bubble}"
    padding: "9px 14px"
  chat-bubble-caller:
    backgroundColor: "{colors.surface-grey}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.bubble}"
    padding: "9px 14px"
  tier-card:
    backgroundColor: "{colors.canvas-grey}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.card}"
    padding: "16px"
  tier-card-selected:
    backgroundColor: "{colors.trade-signal-blue-tint}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.card}"
    padding: "16px"
---

# Design System: Hey Kevin

## 1. Overview

**Creative North Star: "The Dispatch Screen"**

Hey Kevin is a tool for people in the middle of work. A plumber under a sink. An electrician on a ladder. An HVAC tech outside in August heat. The phone buzzes. They look at the screen for one second and decide: real lead or noise? The entire design system exists to make that decision instant.

This system uses iOS conventions as its foundation, not as its ceiling. Standard navigation, standard list styles, standard tab bars — then deliberate brand moments at the exact points where decisions happen. The call's status is unmissable (a single saturated green dot). The primary action is oversized and deeply colored. Everything else steps back. The design does not reward lingering — it rewards the fastest possible glance.

What this system is not: a consumer companion app with rounded-everything softness, bouncy animations, and pastel playfulness. It's not a startup tool's dark slab of tightly packed rows. It's a dispatch screen — designed to be read from across the room, operated with a thumb that may have grease on it, trusted by someone who has no patience for UI.

**Key Characteristics:**
- Flat tonal surfaces — no shadows, depth through saturation contrast only
- Two-signal color system: Trade Signal Blue (Kevin, information) and Clear Line Green (live, go)
- SF Pro throughout — no custom fonts; Dynamic Type supported unconditionally
- 52pt+ tap targets on all primary and critical actions
- High ambient contrast: readable on a smudged screen in direct sunlight

## 2. Colors: The Dispatch Palette

Two signal colors anchor every decision moment. Everything else steps back to grey.

### Primary
- **Trade Signal Blue** (`#007AFF`): Kevin's voice and identity. Kevin's chat bubbles use this at 12% tint (`#007AFF1F`). Active links, selected state tint, tonal highlights where Kevin is the actor. The color that says "Kevin is handling this."

### Secondary
- **Clear Line Green** (`#34C759`): The go signal. Used exclusively for the Pick Up CTA, the Live indicator dot, Connected status, and success confirmation. When you see green, you pick up. Its scarcity is its power.

### Tertiary
- **Warning Orange** (`#FF9500`): Trial expiry banners, cautionary states, "Kevin is taking a message" status (orange dot). Applied as 12% tint for container backgrounds. Never used for primary actions.

### Neutral
- **Error Red** (`#FF3B30`): Error states, destructive action labels (Ignore tint), failure messages.
- **Canvas Grey** (`#F2F2F7`): Main view background and tier card default fill. The iOS `systemGroupedBackground` equivalent.
- **Surface Grey** (`#E5E5EA`): Caller chat bubbles, secondary container surfaces. Reads as distinct from Canvas Grey without a shadow.
- **Mid Grey** (`#C7C7CC`): Dividers, avatar fill, disabled borders.
- **Text Primary** (`#1C1C1E`): All primary label text.
- **Text Secondary** (`#8E8E93`): Supporting copy, subheadings, metadata captions.

### Named Rules
**The Two-Signal Rule.** Blue means Kevin. Green means go. These colors never swap roles, never appear at full saturation in the same element, and never compete on screen. The rarity of green — appearing only when a live call needs a decision — is what makes the Pick Up button unmissable.

**The Tint Rule.** Signal colors (blue, green, orange) appear at full saturation only on interactive elements and status indicators. Container fills use the ×12% tint variant to indicate state without competing with the CTA. Never a full-saturation panel background.

## 3. Typography

**Display/Title Font:** SF Pro Display (system-ui, -apple-system, sans-serif)
**Body Font:** SF Pro Text (system-ui, -apple-system, sans-serif)
**Label/Mono:** SF Pro Text with `tnum` feature enabled (call timer, phone numbers)

**Character:** The system font is the design choice. SF Pro is legible at distance, in sunlight, at any Dynamic Type size. Hierarchy comes from weight and scale, never from font-switching. The result is a system that feels like a native tool — because it is.

### Hierarchy
- **Title2** (700, 22pt, 1.2 leading): Paywall hero, modal headers. One instance maximum per screen.
- **Title3** (600, 20pt, 1.25 leading): Caller name on the live screen, section headers in onboarding. The highest level in operational UI.
- **Headline** (600, 17pt, 1.3 leading): Tier card names, primary button labels, navigation commit actions.
- **Body** (400, 17pt, 1.4 leading): Not currently in heavy use — subheadline covers most body text.
- **Subheadline** (400–500, 15pt, 1.4 leading): The workhorse. List rows, status descriptions, quick-reply options, most text a user reads during a call.
- **Caption** (400, 12pt, 1.3 leading, +0.3pt tracking): Speaker labels above chat bubbles, metadata, footnote/legal copy.
- **Label-Mono** (400, 15pt, `tnum`): Call timer and phone numbers. Monospaced digits prevent layout shift as values change.

### Named Rules
**The Dynamic Type Rule.** Every text element scales with Dynamic Type. No fixed-size text in operational views. A contractor with poor vision may run at Accessibility XL — the screen must reflow correctly at every size.

## 4. Elevation

This system is flat. No box shadows on cards, panels, or buttons at rest. Depth is expressed through tonal contrast: Surface Grey caller bubbles read as distinct from the Canvas Grey background without any shadow. Selected tier cards lift into visibility through the Trade Signal Blue tint, not elevation.

The one exception is the action bar at the bottom of the live call screen: iOS `.ultraThinMaterial` (system blur). This is a functional separator — it signals that the action layer is distinct from the live transcript below — not decoration. Never replicated with a CSS blur effect for aesthetic reasons.

### Named Rules
**The Flat-by-Default Rule.** If you're reaching for a shadow, use a background tint instead. Shadows imply permanent architectural elevation; tints imply state. Hey Kevin's surfaces change state constantly (selected, live, idle, expired) — tints are the right tool.

## 5. Components

### Buttons
Shape: generously curved, never pill-shaped (too consumer-soft), never square (too harsh). Primary actions at `14px` radius, secondary and card-adjacent actions at `12px`.

- **Primary CTA:** Clear Line Green (`#34C759`) fill, white headline text, full-width, 14px radius, 14pt vertical padding. Used for Pick Up, Subscribe, and any irreversible commit. One per screen.
- **Secondary (Bordered):** Transparent fill, 1pt system border, Trade Signal Blue label. Used for Text Reply, secondary navigation, option selection.
- **Destructive:** Transparent fill, Error Red label. Used for Ignore, cancel paths. Never a filled destructive button.
- **Ghost (text-only):** Text Secondary color. Skip/dismiss links only ("Continue without subscribing"). Must visually recede behind the primary CTA.
- **Minimum tap target:** 52pt height for primary, secondary, and destructive. Ghost links: 44pt.

### Chat Bubbles (Signature Component)
The core display of the live screening conversation. Mirror layout: caller left-aligned, Kevin right-aligned — matching iMessage conventions contractors already use.

- **Kevin bubble:** `trade-signal-blue-tint` background (blue at 12%), `text-primary` label, 18px continuous radius, 14pt horizontal / 9pt vertical padding.
- **Caller bubble:** `surface-grey` background (`#E5E5EA`), same dimensions and radius.
- **Speaker label:** `caption2 semibold` (11pt, 600 weight) above each bubble. Trade Signal Blue for Kevin, Text Secondary for Caller.
- Bubbles animate in with a short ease-out transition: ≤150ms, no bounce, no scale pop.

### Cards / Tier Cards
- **Corner Style:** 12px radius (rounded-card)
- **Default Background:** Canvas Grey (`#F2F2F7`)
- **Selected Background:** Trade Signal Blue tint (`#007AFF` at 8%)
- **Shadow Strategy:** None. Selection state communicated by tint only.
- **Border:** None at default. A 1pt Trade Signal Blue border may be added to selected state when tint alone fails WCAG contrast requirements.
- **Internal Padding:** 16pt uniform

### Inputs / Fields
- **Style:** iOS `.roundedBorder` text field. System background. No custom glow or ring — trust the platform for focus treatment.
- **Focus:** Platform keyboard appearance. Default iOS treatment.
- **Error:** Error Red label below the field, never inline placeholder text.
- **Disabled:** Reduced opacity (system `.disabled` modifier).

### Navigation
- **Tab Bar:** Three tabs — Live (waveform), Recents (clock), Settings (gear). System SF Symbols only. Badge counts for unread calls and active call state.
- **Navigation Bar:** `.inline` title display for operational screens. `.large` title for top-level tab views only.
- **Toolbar Actions:** `.subheadline`-weight text button, right-aligned. One per screen. ("Mark All Read", "Done")

### Status Indicators
7pt filled circle, semantic color only (green = live/go, orange = taking message, no dot for idle). Always paired with a text label in Caption weight — color alone never carries meaning.

## 6. Do's and Don'ts

### Do:
- **Do** use Clear Line Green (`#34C759`) exclusively for go-signals: Pick Up CTA, active call dot, Connected status. It is the one color a contractor trusts without thinking.
- **Do** maintain 52pt minimum tap targets on all interactive elements in the Live screen, Paywall, and Onboarding. Contractors tap with thumbs, often under pressure.
- **Do** use `.subheadline` (15pt) as the default text size for operational list rows and status text — it holds at arm's length and in bright light better than caption alternatives.
- **Do** pair every status color with a text label. A green dot alone fails color-blind users and anyone glancing in direct sunlight.
- **Do** express selection and active state through background tints (8–12% opacity signal color). Tints adapt to dark mode correctly; hard shadows and borders do not.
- **Do** treat Dynamic Type as a hard requirement. Every operational screen must reflow at Accessibility XL.
- **Do** use `.ultraThinMaterial` for the action bar only — one structural separator between the transcript and the action layer.

### Don't:
- **Don't** use gradient fills or `background-clip: text` gradient effects. Hey Kevin is not a consumer app designed to be screenshot-shared.
- **Don't** use glassmorphism, multi-layer blurs, or decorative shadows. The one blur in the system is structural; everything else is flat.
- **Don't** build anything that feels made for teenagers or consumer entertainment: bouncy animations, pastel playfulness, emoji-heavy copy, confetti on subscription.
- **Don't** let Trade Signal Blue and Clear Line Green appear at full saturation in the same element. They have separate roles; overlap dilutes both.
- **Don't** use `border-left` directional stripe accents on list items or cards to indicate state. Use background tints or full-border treatments.
- **Don't** use nested cards. If a card needs to contain another card, flatten the hierarchy.
- **Don't** make contractors aim. Destructive or commit actions (Ignore, Subscribe, Pick Up) require broad thumb targets — 52pt height, 20pt outer padding minimum.
- **Don't** use a "smart home concierge" aesthetic: premium-soft whites, hairline typography, decorative negative space. This is a job tool.
- **Don't** patronize trades workers with excessive onboarding handholding. Show the function. Trust the user to operate it.
