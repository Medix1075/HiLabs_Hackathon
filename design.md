---
version: "alpha"
name: "Neumorphism"
description: "Neumorphic UI with soft 3D effects. Ideal for modern apps, dashboards, health/fitness, productivity. AI-ready template."
colors:
  primary: "#C8E0F4"
  secondary: "#F5E0E8"
  tertiary: "#E8E8E8"

typography:
  h1:
    fontFamily: -apple-system, sans-serif
    fontSize: 2.25rem
    fontWeight: 700
  body-md:
    fontFamily: -apple-system, sans-serif
    fontSize: 1rem
    fontWeight: 400
  label-caps:
    fontFamily: -apple-system, sans-serif
    fontSize: 0.75rem
    fontWeight: 500
rounded:
  sm: 14px
  md: 28px
  lg: 42px
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    rounded: "{rounded.sm}"
    padding: 12px
---

## Overview

Neumorphic UI with soft 3D effects. Ideal for modern apps, dashboards, health/fitness, productivity. AI-ready template. It started with a single Dribbble shot. In late 2019, Ukrainian designer Alexander Plyuto posted a soft, pillowy UI concept that looked like buttons extruded from clay. The internet lost its mind. By early 2020, "neumorphism" was everywhere — every design blog, every Twitter thread, every junior designer's portfolio. The name itself (new + skeuomorphism) promised a middle ground between the dead realism of iOS 6 and the sterile flatness that replaced it.

The backlash came fast. Accessibility experts pointed out the obvious: when your entire UI relies on subtle shadow differences against a same-color background, contrast ratios tank. Buttons become invisible to low-vision users. Interactive states blur into decoration. WCAG compliance? Good luck.

Six years later, neumorphism occupies a weird niche. Nobody ships a full neumorphic interface anymore — that experiment failed. But the technique survived as an accent. A toggle here, a card there, a dashboard widget that needs to feel tactile. It works best in controlled doses, on personal projects or internal tools where you control the audience. The hype died. The aesthetic didn't.

- Density: 5/10 — Balanced
- Variance: 4/10 — Moderate
- Motion: 4/10 — Subtle

- **Style:** Soft, Embossed, Monochromatic, Rounded
- **Keywords:** Soft UI, embossed, debossed, convex, concave, light source, subtle depth, rounded (12-16px), monochromatic
- **Era:** 2020s Modern
- **Light/Dark:** ✓ Full / ◐ Partial

## Colors

- **Soft Blue** (#C8E0F4) — Accent highlight, links and focus states
- **Soft Pink** (#F5E0E8) — Primary text color
- **Soft Grey** (#E8E8E8) — Secondary text, borders, muted elements


## Typography

- **Display / Hero:** System UI stack (-apple-system, sans-serif) — Weight 700, tight tracking, used for headline impact
- **Body:** System UI stack (-apple-system, sans-serif) — Weight 400, 16px/1.6 line-height, max 72ch per line
- **UI Labels / Captions:** System UI stack (-apple-system, sans-serif) — 0.875rem, weight 500, slight letter-spacing
- **Monospace:** JetBrains Mono — Used for code, metadata, and technical values

Scale:
- Hero: clamp(2.5rem, 5vw, 4rem)
- H1: 2.25rem
- H2: 1.5rem
- Body: 1rem / 1.6
- Small: 0.875rem


## Layout

- **Grid:** CSS Grid primary. Max-width containment: 1280px centered with 1.5rem side padding.
- **Spacing rhythm:** Balanced. Base unit: 0.5rem (8px).
- **Section vertical gaps:** clamp(4rem, 8vw, 8rem).
- **Hero layout:** Split-screen (text left, visual right).
- **Feature sections:** Zig-zag alternating text+image rows. No 3-equal-columns.
- **Mobile collapse:** All multi-column layouts collapse below 768px. No horizontal overflow.
- **z-index contract:** base (0) / sticky-nav (100) / overlay (200) / modal (300) / toast (500).


## Elevation & Depth

Soft box-shadow (multiple: -5px -5px 15px, 5px 5px 15px), smooth press (150ms), inner subtle shadow

- **Physics:** Ease-out curves, 200-300ms duration. Smooth and predictable.
- **Entry animations:** Fade + translate-Y (16px → 0) over 420ms ease-out. Staggered cascades for lists: 80ms between items.
- **Hover states:** Subtle color shift + shadow adjustment over 200ms.
- **Page transitions:** Fade only (200ms).
- **Performance:** Only transform and opacity animated. No layout-triggering properties.


## Shapes

Base corner radius: 14px. See rounded tokens in front matter for the full scale.


## Components

- **Primary Button:** Rounded (14px) shape. Accent color fill. Hover: 8% darken + subtle lift shadow. Active: -1px translate tactile press. Font weight 600. No outer glows.
- **Secondary / Ghost Button:** Outline variant. 1.5px border in muted color. Text in primary color. Hover: subtle background fill.
- **Cards:** Rounded (14px) corners. Surface background. Subtle shadow (0 2px 12px rgba(0,0,0,0.06)). 1px border stroke.
- **Inputs:** Label above input. 1px border stroke. Focus ring: 2px accent color offset 2px. Error text below in semantic red. No floating labels.
- **Navigation:** Primary surface background. Active item: accent color indicator. Font weight 500 when active.
- **Skeletons:** Shimmer animation matching component dimensions. No circular spinners.
- **Empty States:** Icon-based composition with descriptive text and action button.


## Do's and Don'ts

- No emojis in UI — use icon system only (Lucide, Heroicons)
- No pure black (#000000) — use off-black or charcoal variants
- No oversaturated accent colors (saturation cap: 80%)
- No 3-column equal-width feature layouts — use zig-zag or asymmetric grid
- No `h-screen` — use `min-h-[100dvh]`
- No AI copywriting clichés: "Elevate", "Seamless", "Unleash", "Next-Gen"
- No broken external image links — use picsum.photos or inline SVG
- No generic lorem ipsum in demos

- Do Rounded corners 12-16px consistent
- Do Multiple shadow layers (2-3)
- Do Pastel color verified
- Do Monochromatic palette checked
- Do Press animation smooth 150ms


## Use Case

Modern apps, Dashboards, Health/Fitness, Productivity

<!-- Source: https://designmd.app/library/neumorphism · designmd.app -->
