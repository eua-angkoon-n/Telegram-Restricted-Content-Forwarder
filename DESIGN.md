# Design System

## Direction

A restrained pastel operations console inspired by soft morning light through pink petals. Direction A uses an asymmetric account rail and a wider forwarding workspace. The interface stays familiar, quiet, and task-first; it does not reproduce game UI or character art.

## Color

All implementation colors use OKLCH. Hyacinth Crimson is the exact CSS equivalent of the user-selected `#D2134B`.

```css
--color-bg: oklch(98.21% 0.0069 304.24);
--color-surface: oklch(100% 0 0);
--color-primary: oklch(55.49% 0.2143 14.10);
--color-primary-hover: oklch(49% 0.19 14.10);
--color-petal: oklch(83.75% 0.0745 1.77);
--color-lavender: oklch(87.40% 0.0512 303.02);
--color-mint: oklch(90.29% 0.0440 187.54);
--color-honey: oklch(89.77% 0.0712 80.61);
--color-ink: oklch(27% 0.055 14.10);
--color-muted: oklch(47% 0.035 304.24);
--color-border: oklch(89% 0.025 304.24);
```

Hyacinth Crimson identifies primary actions, focus, headings, and important stopped/error states. Aqua Mint identifies healthy/active/success states. Honey is warning-only. Color is always paired with text or an icon.

## Typography

Use the native humanist sans stack `ui-rounded, "Segoe UI", system-ui, sans-serif` for fast loading and a friendly product tone. The fixed scale is 0.75rem, 0.875rem, 1rem, 1.25rem, and 1.75rem. Body text is at least 1rem with 1.5 line height. Counters and timestamps use tabular numerals.

## Layout

Desktop uses a 18rem account rail and a flexible work area inside a 90rem maximum-width shell. On smaller first-run screens, account setup comes first; after an account exists, the forwarding workspace moves first. Spacing follows a 4px-derived scale: 0.25rem, 0.5rem, 0.75rem, 1rem, 1.5rem, 2rem, 3rem. The four-petal motif alone uses a 0.125rem geometric gap.

Use one bounded forwarding work surface, a separate account rail, full-perimeter hairline borders, and dividers. Avoid nested cards and repeated icon-card grids.

## Components

- Buttons and fields have a minimum 44px target, visible labels, and 12px maximum radius.
- Primary buttons use Hyacinth Crimson with white text; destructive controls are outlined until confirmed.
- Status strip combines icon, label, and explanatory copy.
- Activity log is an `aria-live` list with timestamps and plain-language messages.
- OTP and 2FA continue inline in Account setup instead of opening modal dialogs.

## Motion

Use 180ms ease-out transitions for hover, focus, and status changes only. Under `prefers-reduced-motion: reduce`, transitions are disabled.

## Accessibility

Target WCAG AA, preserve browser zoom, provide a skip link and visible focus rings, use semantic headings and form labels, announce async results, and never rely on color alone.
