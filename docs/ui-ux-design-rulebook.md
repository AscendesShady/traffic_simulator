# UI/UX Design Rulebook for AI Coding LLMs

A concise, evidence-based UI/UX implementation guide synthesized from professional design literature.

---

## 1. Core UI/UX Principles

### 1.1 Gestalt Laws of Visual Perception
Humans perceive visual elements as unified, organized wholes rather than isolated parts (*Ultimate Figma for UI/UX Design*, Ch. 2, pp. 31–35):
* **Proximity**: Elements placed close to one another are perceived as a related group. Use grouped containers or tight spacing to establish functional relationships.
* **Similarity**: Elements sharing visual attributes (color, shape, size) are understood to serve identical functions (e.g., all primary action buttons sharing the same background fill).
* **Common Region**: Enclosing components within a distinct boundary or background card solidifies their relationship.
* **Figure-Ground**: Maintain strong visual contrast between foreground visual elements (text, buttons) and background surfaces to maximize legibility.
* **Symmetry & Order**: The human brain prefers balanced layout structures. Symmetrical arrangements evoke stability, while deliberate asymmetry creates focal emphasis.

### 1.2 System-Real World Alignment & Mental Models
* **Jakob Nielsen’s 2nd Usability Heuristic**: Interface concepts, terminology, and controls must map directly to concepts users already understand from real-world experiences (*101 UX Principles*, Principle 13, pp. 53–56).
* **Affordances & Signifiers**: Interactive controls must visually communicate their interactability (e.g., raised borders, subtle drop shadows, distinct background fills). Avoid non-interactive elements that mimic clickable controls.

### 1.3 Cognitive Load & Purpose-Driven Design
* **Primary Objective**: Interfaces must be self-explanatory and task-oriented, enabling users to accomplish goals with minimal friction (*Roots of UI/UX Design*, p. 39; *UX and UI Strategy*, Ch. 3, p. 116).
* **Nielsen Norman Group Rule**: Visual hierarchy controls the delivery of the user experience. Unclear layouts increase cognitive burden and user bounce rates (*Ultimate UI/UX Design for Professionals*, Ch. 7, p. 245).

---

## 2. Layout & Visual Hierarchy

### 2.1 Eye-Scanning Patterns
* **F-Pattern**: Users scan text-heavy interfaces (articles, documentation, search results) top-to-bottom and left-to-right (*Roots of UI/UX Design*, p. 95; *Ultimate UI/UX Design for Professionals*, Ch. 7, pp. 246–247):
  1. Horizontal sweep across top headline/navigation.
  2. Short horizontal sweep across subheadings or key bullet points.
  3. Vertical downward scan along the left margin.
* **Z-Pattern**: Users scan visual or landing pages with low text density along a 'Z' shape:
  1. Top-left (Logo/Brand) to Top-right (Primary Nav/CTA).
  2. Diagonal sweep down to Bottom-left.
  3. Horizontal sweep across Bottom-right (Final Call to Action).

### 2.2 Grids, Alignment & Spacing
* **Grid Systems**: Always align layouts to a structured column grid system to ensure visual consistency (*UI Design Principles*, p. 32; *Ultimate Figma for UI/UX Design*, p. 39).
* **Alignment Rules**:
  * Left-align textual copy in Left-to-Right (LTR) languages to support natural reading habits.
  * Avoid justified text alignment on web pages as it creates uneven character gaps.
* **Whitespace & Breathing Room**: Use generous whitespace around high-priority components to reduce visual noise and focus attention (*Ultimate UI/UX Design for Professionals*, Ch. 7, p. 252).
* **Border Radius Consistency & Nesting Rule**:
  * Outer card containers must have a larger border radius than nested child elements (e.g., Outer Card = `16px`, Inner Image/Button = `8px`) (*Ultimate UI/UX Design for Professionals*, Ch. 7).
  * Navigation bars and primary containers should maintain rounded corners (`border-radius > 8px`) for an approachable aesthetic (*Roots of UI/UX Design*, p. 140).

---

## 3. Navigation & Information Architecture

### 3.1 Information Architecture Pillars & Models
* **3 Pillars of IA**: Balance **Content** (what is provided), **Context** (business/tech constraints), and **Users** (goals and mental models) (*Ultimate UI/UX Design for Professionals*, Ch. 5, p. 177).
* **Structure Models**:
  * *Single-Page Model*: Scrolling-based layout for focused narrative landing pages.
  * *Flat Model*: Ensures all primary pages are accessible within 1–2 clicks from the home view (ideal for small-to-medium apps).
  * *Hierarchical Model*: Multi-level primary and secondary categories for complex enterprise applications.

### 3.2 Navigation Controls & Rules
* **Mobile Bottom Bar Limit**: Restrict bottom mobile navigation bars to a **maximum of 5 tabs**. Exceeding 5 tabs reduces individual touch targets below minimum ergonomic thresholds (*Roots of UI/UX Design*, p. 136).
* **Avoid Hiding Primary Items**: Do not collapse primary desktop navigation into a "Hamburger" menu when screen real estate permits visible items (*101 UX Principles*, Principle 29, p. 118).
* **Subsection Categorization**: Chunk long menu lists into logical sub-categories to respect human short-term memory limits (*101 UX Principles*, Principle 31, p. 126).
* **Breadcrumb Navigation**: Implement breadcrumbs (`Home > Category > Subcategory > Item`) on multi-level sites to provide orientation and rapid back-tracking (*101 UX Principles*, Principle 77, pp. 317–318).
* **Footer Navigation**: Mirror essential primary links in the footer section to eliminate "dead-end" page scrolling (*101 UX Principles*, pp. 136–137).
* **Skip Links**: Include an accessible `#skip-to-content` link before top headers for screen readers and keyboard users (*101 UX Principles*, Principle 68, p. 282).

---

## 4. Components & Interaction

### 4.1 Buttons & Interactive Controls
* **Touch Target Sizes**: Interactive controls must maintain a minimum touch target size of **44x44pt (44x44px)** on mobile/touch interfaces (*101 UX Principles*, Principle 73, p. 299; *UI Design Principles*, p. 120).
* **Spacing Between Targets**: Provide a minimum gap/padding of **8px to 10px** (or ~3mm) between adjacent touch targets to prevent accidental misclicks (*Roots of UI/UX Design*, pp. 259–260).
* **Full Hit Area**: Make the entire button bounding box clickable, not just the inner text label (*101 UX Principles*, Principle 15, p. 63).
* **Button Hierarchy**: Limit views to **one Primary Action Button** per card or screen area. Secondary and tertiary actions must use outlined or text-only variants (*Roots of UI/UX Design*, pp. 92–93; *UI Design Principles*, p. 120).
* **Desktop Hover Affordance**: Force `cursor: pointer` on hoverable interactive desktop controls (*101 UX Principles*, Principle 15, p. 63).
* **Standard Controls**: Never invent custom, non-standard controls (e.g., 3D color wheels, hold-to-confirm buttons, circular volume dials) when standard HTML controls exist (*101 UX Principles*, Principle 16, pp. 65–66).
* **Ellipsis Indicator (...)**: Append an ellipsis to button/menu labels if triggering the control opens a secondary dialog or requires additional user input before execution (*101 UX Principles*, Principle 12, p. 49).

### 4.2 Search Patterns
* **Standard Search Control**: Search components should feature an explicit text input paired with a button labeled "Search" or a recognized magnifying glass icon (*101 UX Principles*, Principle 17, pp. 67–68).
* **Result Order**: Display the most relevant search result at the absolute top of the results list (*101 UX Principles*, Principle 95, p. 383).

### 4.3 Form Controls & Input Choice
* **Dropdown Threshold**: Do not use dropdown select menus if there are fewer than 5 options. Use radio buttons or segmented button groups instead (*101 UX Principles*, Principle 20, p. 77).
* **Sliders vs Numeric Inputs**: Sliders should only be used for non-quantifiable or relative adjustments (e.g., volume, brightness). Use numeric input fields for precise integers (*101 UX Principles*, Principles 18 & 19, pp. 73–75).

---

## 5. Typography & Color

### 5.1 Typography Rules
* **Font Family Limit**: Use a maximum of **two typefaces** per application (one for headings/titles, one for body/UI text) (*101 UX Principles*, Principle 8, p. 33).
* **Native System Fonts**: Leverage system fonts (`system-ui`, `-apple-system`, `sans-serif`) as fast, reliable defaults (*101 UX Principles*, Principle 9, p. 35).
* **Type Hierarchy Scale**: Limit the font size scale to 3 distinct sizes within standard copy blocks (Heading, Subheading, Body) (*Roots of UI/UX Design*, p. 179).
* **Body Copy Standards**: Default body text size should be at least **16px** with a line height of **1.5 (150%)** (*101 UX Principles*, Principle 11, p. 45; *Roots of UI/UX Design*, p. 57).
* **Avoid Decorative Primary Text**: Script and overly ornate typefaces must never be used for primary interface copy or body text (*Roots of UI/UX Design*, p. 57).

### 5.2 Color & Contrast Rules
* **WCAG Contrast Ratios**:
  * **Minimum Contrast (WCAG AA)**: `4.5:1` ratio for body copy and UI controls (*101 UX Principles*, Principle 64, p. 272; *Ultimate Figma for UI/UX Design*, p. 74).
  * **Enhanced Contrast (WCAG AAA)**: `7.5:1` ratio for optimal legibility (*101 UX Principles*, Principle 64, p. 272).
* **Never Pure Black**: Avoid pure black (`#000000`) text or dark theme backgrounds due to harsh visual contrast and eye strain. Use dark charcoal or deep gray shades (e.g., `#121212`, `#1A1A1A`) (*Roots of UI/UX Design*, Ref 3, p. 260; *UI Design Principles*, p. 77).
* **Semantic Functional Colors**:
  * **Primary / Info**: Blue (`#3E8EF4` or similar)
  * **Success**: Green (`#64BC26` or similar)
  * **Warning**: Orange / Yellow (`#FD9900` or similar)
  * **Error / Danger**: Red (`#FE2712` or similar) (*Roots of UI/UX Design*, p. 92; *UI Design Principles*, p. 77, 100).
* **60-30-10 Color Distribution**:
  * **60%**: Dominant neutral background color.
  * **30%**: Secondary structural surfaces (cards, sidebars, text).
  * **10%**: Accent color reserved strictly for interactive primary actions and key highlights (*UI Design Principles*, p. 77).
* **Secondary Icon Opacity**: Secondary/decorative icons in navigation or list rows should use a 50%–70% opacity tint of the primary color rather than full text contrast (*Roots of UI/UX Design*, p. 140, p. 242).
* **Color Neutrality for Accessibility**: Never rely on color alone to convey state or information. Pair color cues with explicit text labels or visual icons (*101 UX Principles*, Principle 69, p. 286).

---

## 6. UI States & Feedback

### 6.1 State Matrix Requirements
Every interactive component (buttons, form inputs, selection cards) must implement explicit visual designs for all 6 core states (*UI Design Principles*, p. 120; *Roots of UI/UX Design*, pp. 93–94):
1. **Default / Enabled**: Normal resting interactive state.
2. **Hover**: Cursor mouse-over feedback.
3. **Active / Pressed**: Mouse-down or tap state.
4. **Focus**: Keyboard focus state featuring a high-contrast focus ring/outline (`focus-visible`).
5. **Progress / Loading**: In-flight processing state with inline spinner or disabled action text.
6. **Disabled**: Non-interactive state with lower opacity/gray fill.

### 6.2 Progress & Loading Patterns
* **Determinate Tasks**: Use a linear progress bar with a percentage or step count indicator when the task duration is known (*101 UX Principles*, Principles 61 & 62, pp. 255–259).
* **Indeterminate Tasks**: Use an animated spinner for tasks of unknown duration or quick micro-actions (*101 UX Principles*, Principle 63, p. 263).
* **Stop on Failure**: Ensure spinners and progress indicators halt immediately if an operation encounters an error (*101 UX Principles*, Principle 63, p. 263).
* **Skeleton Screen Loading**: Use gray content placeholders/skeletons during initial data fetching to prevent visual layout shifts ("element shunt") (*101 UX Principles*, Principle 45, p. 187; *UI Design Principles*, p. 280).

### 6.3 Empty States
* **Mandatory Requirements**: When a data view contains zero items (new account, empty cart, empty search results), show a dedicated empty state view (*101 UX Principles*, Principle 26, pp. 105–107; *Roots of UI/UX Design*, p. 129):
  1. An illustrative icon or graphic.
  2. Clear, task-oriented headline and body explanation.
  3. A primary Call-to-Action button guiding the user's next step (e.g., "Create First Project").
* **Dismissable Onboarding**: Ensure onboarding banners and "getting started" tip cards include an explicit, easy-to-find dismiss/skip control (*101 UX Principles*, Principle 27, p. 110).

### 6.4 Errors, Modals & Destructive Actions
* **Destructive Safety**: Provide an "Undo" pattern for destructive actions or prompt for confirmation via a modal dialog (*101 UX Principles*, Principle 21, p. 81).
* **Modal Discipline**: Modal overlays are **blocking actions**. Use modals *only* when the system requires an immediate decision before proceeding. Do not use modals for non-critical status updates or background alerts (*101 UX Principles*, Principle 97, pp. 391–393).

---

## 7. Forms & Data Interfaces

### 7.1 Form Usability Rules
* **Validation Timing**: Perform validation as soon as possible (inline on input blur or submit). Never return a user to a form without highlighting the exact field requiring attention and providing clear, actionable error text (*101 UX Principles*, Principles 51 & 52, pp. 208–212; *Roots of UI/UX Design*, p. 101).
* **Preserve Form Inputs**: Never clear user-entered data upon validation error or page reload (*101 UX Principles*, Principle 43, pp. 178–179).
* **Explicit Labels**: Always place clear labels above or adjacent to inputs. Never rely on placeholder text as a replacement for form labels (*Ultimate Figma for UI/UX Design*, p. 162).
* **Show Password Toggle**: Include a toggle control to show/hide password text in authentication fields (*UI Design Principles*, p. 135).
* **Multi-Step Wizards**: For forms containing 5 or more fields, divide inputs into sequential steps using a progressive wizard pattern with step numbers (*UI Design Principles*, p. 135; *Roots of UI/UX Design*, pp. 245–246).
* **Native Input Types**: Use semantic HTML input types (`type="email"`, `type="tel"`, `type="number"`, `type="date"`) to invoke optimal native virtual keypads and pickers (*101 UX Principles*, Principles 22 & 46, p. 87, p. 190).
* **Payment Inputs**: Capture bare minimum details for payment fields; never force arbitrary decimal places on currency inputs (*101 UX Principles*, Principles 57 & 59, p. 233, p. 245).

### 7.2 Data Tables & Data Visualization
* **Table Column Alignment**:
  * **Textual Data**: Left-align textual columns to fit natural LTR reading (*Roots of UI/UX Design*, p. 207).
  * **Numeric Data**: Right-align numeric columns to enable easy visual alignment and comparison of values (*Roots of UI/UX Design*, p. 207).
  * **Status Badges / Icons**: Center-align icons and status badges (*Roots of UI/UX Design*, p. 207).
* **Table Spacing & Sticky Headers**: Maintain comfortable row line-heights and padding; implement sticky headers on scrollable data tables (*Roots of UI/UX Design*, p. 207; *Ultimate UI/UX Design for Professionals*, Ch. 10).
* **Chart Simplicity**: Avoid 3D effects on charts as they distort visual proportion. Use clean grid lines and hover tooltips for expanded data details (*Roots of UI/UX Design*, pp. 200–202).

---

## 8. Accessibility & Responsive Design

### 8.1 Accessibility (a11y) Standards
* **Color & Contrast**: Maintain WCAG AA compliance (`>= 4.5:1` contrast) across all text and interactive state colors (*101 UX Principles*, Principle 64, p. 272).
* **Keyboard Navigation**: Ensure all interactive controls are accessible via `Tab` key navigation with a clear visual focus indicator (`focus-visible`) (*Ultimate Figma for UI/UX Design*, p. 166, p. 196).
* **Contextual Link Text**: Never use generic link text such as "click here" or "read more". Use self-descriptive labels like "Download Q3 Financial Report (PDF)" (*101 UX Principles*, Principle 67, p. 280).
* **ARIA & Screen Readers**: Use semantic HTML tags (`<header>`, `<nav>`, `<main>`, `<article>`, `<aside>`, `<footer>`) and appropriate `aria-invalid`, `aria-describedby`, and `aria-label` attributes (*Ultimate Figma for UI/UX Design*, p. 166).
* **Reduced Motion**: Respect system settings for `prefers-reduced-motion` by disabling non-essential animations (*Ultimate Figma for UI/UX Design*, p. 197).

### 8.2 Responsive Web Design (RWD) Rules
* **Mobile-First Paradigm**: Write CSS starting from mobile device viewports, progressively enhancing layouts for tablet and desktop breakpoints (*Ultimate UI/UX Design for Professionals*, Ch. 9, p. 272; *Ultimate Figma for UI/UX Design*, pp. 199–203).
* **Fluid Layouts**: Use fluid grids, flexible images (`max-width: 100%; height: auto;`), and CSS Flexbox/Grid.
* **Touch Targets**: Enforce a minimum touch target size of `44x44px` on screen widths `< 768px` (*101 UX Principles*, Principle 73, p. 299).

---

## 9. UI/UX Anti-Patterns & Dark Patterns

### 9.1 Dark Patterns to Avoid
* **Deceptive Opt-Outs**: Hiding opt-out options in small, low-contrast text while rendering opt-in actions in vibrant primary colors (*101 UX Principles*, Principle 101, pp. 409–412; *Ultimate UI/UX Design for Professionals*, Ch. 7).
* **Misdirection**: Manipulating visual hierarchy to divert attention away from critical details or pricing terms (*Ultimate UI/UX Design for Professionals*, Ch. 7).
* **Lazy / Forced Registration**: Blocking browsing or feature exploration with mandatory account creation popups.

### 9.2 Usability Anti-Patterns
* **Element Shunt / Layout Shift**: Failing to allocate space for images/content during load, causing the layout to jump when assets render (*101 UX Principles*, Principle 45, p. 187).
* **Linear Spinner Loops**: Progress animations that meander to 100% and restart repeatedly without real status updates (*101 UX Principles*, Principle 63, pp. 255–263).
* **Form Data Wipe**: Clearing form inputs upon server-side validation error or accidental refresh (*101 UX Principles*, Principle 43, pp. 178–179).
* **Unnecessary Modals**: Interrupting user workflows with modal popups for non-blocking notifications (*101 UX Principles*, Principle 97, p. 391).

---

## 10. Coding LLM Design Rules (CSS / Code Specifications)

When generating code for user interfaces, follow these exact specification rules:

### 10.1 CSS Custom Properties Baseline
```css
:root {
  /* Color System (60-30-10 Rule & Dark Grays) */
  --bg-neutral: #f8f9fa;
  --surface-card: #ffffff;
  --text-primary: #1a1a1a;   /* Never pure #000000 */
  --text-secondary: #595959;
  
  --color-primary: #2563eb;   /* 10% Accent */
  --color-primary-hover: #1d4ed8;
  --color-success: #16a34a;
  --color-warning: #d97706;
  --color-danger: #dc2626;

  /* Typography Scale */
  --font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  --font-size-body: 1rem;     /* 16px default */
  --line-height-body: 1.5;

  /* Spacing & Borders */
  --radius-outer: 1rem;       /* 16px card container */
  --radius-inner: 0.5rem;     /* 8px child elements */
  --touch-target-min: 44px;   /* Minimum 44x44px target */
  --gap-min: 0.5rem;          /* Minimum 8px spacing */
}
```

### 10.2 Component CSS Implementation Rules
1. **Buttons**:
   ```css
   .btn {
     min-width: var(--touch-target-min);
     min-height: var(--touch-target-min);
     padding: 0.75rem 1.25rem;
     border-radius: var(--radius-inner);
     font-size: var(--font-size-body);
     cursor: pointer;
     display: inline-flex;
     align-items: center;
     justify-content: center;
     gap: var(--gap-min);
     transition: background-color 0.15s ease, box-shadow 0.15s ease;
   }

   .btn:focus-visible {
     outline: 3px solid var(--color-primary);
     outline-offset: 2px;
   }

   .btn:disabled {
     opacity: 0.6;
     cursor: not-allowed;
   }
   ```

2. **Form Inputs**:
   ```css
   .form-group {
     display: flex;
     flex-direction: column;
     gap: 0.375rem;
     margin-bottom: 1rem;
   }

   .form-label {
     font-weight: 600;
     color: var(--text-primary);
   }

   .form-input {
     min-height: var(--touch-target-min);
     padding: 0.625rem 0.875rem;
     border: 1px solid #d1d5db;
     border-radius: var(--radius-inner);
     font-size: var(--font-size-body);
   }

   .form-input[aria-invalid="true"] {
     border-color: var(--color-danger);
   }

   .error-message {
     color: var(--color-danger);
     font-size: 0.875rem;
   }
   ```

3. **Data Tables**:
   ```css
   .data-table th,
   .data-table td {
     padding: 0.75rem 1rem;
     line-height: 1.4;
   }

   .data-table .col-text { text-align: left; }
   .data-table .col-number { text-align: right; font-variant-numeric: tabular-nums; }
   .data-table .col-badge { text-align: center; }
   ```

---

## 11. Final UI/UX Checklist for AI Agents

Before delivering code for any interface, verify against this checklist:

- [ ] **Contrast**: Is text contrast $\ge 4.5:1$? Is pure black (`#000000`) avoided for body text and dark backgrounds?
- [ ] **Touch Targets**: Are all interactive elements at least `44x44px` with at least `8px` spacing between targets?
- [ ] **State Completeness**: Do controls feature Default, Hover, Active, Focus, Progress, and Disabled visual states?
- [ ] **Keyboard Focus**: Is a clear `:focus-visible` outline implemented for all inputs and buttons?
- [ ] **Typography**: Are there $\le 2$ typefaces? Is body copy $\ge 16\text{px}$ with a `1.5` line height?
- [ ] **Form Labels**: Does every input have a visible `<label>` tag (not just a placeholder)?
- [ ] **Validation Feedback**: Do form errors highlight specific fields and state clear error messages without wiping input data?
- [ ] **Empty Views**: Does any data view implement a clear empty state with graphic, text, and primary CTA button?
- [ ] **Accessibility (a11y)**: Are links descriptive? Are ARIA attributes and semantic HTML used correctly?
- [ ] **Responsive Design**: Does the layout adapt fluidly across mobile, tablet, and desktop viewports?

---

## 12. Source References

1. **Grant, Will.** *101 UX Principles, 2nd Edition*. Packt Publishing.
   * Key Principles: 8 (Typefaces), 9 (System Fonts), 11 (Body Size), 12 (Ellipsis), 13 (Real World Affordances), 15 (Clickable Button Area & Pointer), 16 (Standard Controls), 17 (Search Pattern), 18–19 (Sliders vs Integers), 20 (Dropdown Limit), 21 (Undo), 22 & 46 (Native Inputs/Pickers), 26–27 (Empty States & Tips), 29 (Hamburger Menu Avoidance), 31 (Subsections), 43 (Form Persistence), 45 (Element Shunt), 51–52 (Form Validation), 57 & 59 (Payments), 61–63 (Progress Bars & Spinners), 64 (Contrast Ratios), 67 (Descriptive Links), 68 (Skip Links), 69 (Color Independence), 73 (Finger-Sized Targets), 77 (Breadcrumbs), 95 (Search Relevance), 97 (Modal Discipline), 101 (Dark Patterns).
2. **Paduraru, Elisa.** *Roots of UI/UX Design: Learn to Develop Intuitive Web Experiences*. Creative Tim.
   * Key Topics: Typography Classification & Alignment (pp. 57, 207), Color Semantics & Pantone/WCAG Systems (pp. 74, 92), Button Roles/States (pp. 92–95), Border Radius & Nav Bars (p. 140), Empty State Illustrations (p. 129), Table Alignment & Padding Rules (p. 207), Mobile Navigation Tab Limits (p. 136).
3. **Filipiuk, Michael.** *UI Design Principles*.
   * Key Topics: Grid Systems & Layout Symmetry (p. 32), Dark Color Contrast & Black Avoidance (p. 77), Color Psychology & Notification Palette (p. 77, 100), 60-30-10 Color Allocation (p. 77), Button/Input 6-State Matrices (p. 120, 135), Multi-Step Form Chunking (p. 135), Skeleton Screens (p. 280).
4. **Deacon, Pamala.** *UX and UI Strategy: A Step-by-Step Guide*.
   * Key Topics: Usability Principles & Task Efficiency (Ch. 3, p. 116), Hierarchical Organization, Consistency in Branding & Layout (Ch. 1).
5. **Sharma, Aditi.** *Ultimate Figma for UI/UX Design*.
   * Key Topics: Gestalt Laws of Perception (Ch. 2, pp. 31–35), Visual Hierarchy & Contrast Checkers (pp. 49–74), Form Accessibility & Keyboard Focus (pp. 162–166), Component Variant States (pp. 187–189), Responsive Auto Layout & Constraints (pp. 199–203).
6. **Kaur, Sharanpreet.** *Ultimate UI/UX Design for Professionals*. Orange Education.
   * Key Topics: Information Architecture 3 Pillars & Models (Ch. 5, pp. 174–188), Visual Hierarchy Patterns (F-Pattern & Z-Pattern) (Ch. 7, pp. 245–247), White Space & Border Radius Hierarchy (Ch. 7, p. 252), UI Design Patterns & Dark Patterns/Misdirection (Ch. 7, pp. 259–264), Mobile-First Responsive Web Design (Ch. 9, p. 272).
