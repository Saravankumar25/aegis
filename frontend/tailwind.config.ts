import type { Config } from "tailwindcss";

// Every colour is a CSS variable so both themes share one class vocabulary.
const rgb = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: rgb("bg"),
        surface: rgb("surface"),
        surface2: rgb("surface-2"),
        edge: rgb("edge"),
        fg: rgb("fg"),
        muted: rgb("muted"),
        "inverse-bg": rgb("inverse-bg"),
        "inverse-fg": rgb("inverse-fg"),
        sev1: rgb("sev-1"),
        sev2: rgb("sev-2"),
        sev3: rgb("sev-3"),
        sev4: rgb("sev-4"),
        ok: rgb("ok"),
        warn: rgb("warn"),
        danger: rgb("danger"),
      },
      fontSize: {
        hero: ["clamp(2.75rem, 8vw, 6.5rem)", { lineHeight: "1.02" }],
        section: ["clamp(2rem, 5vw, 3.75rem)", { lineHeight: "1.06" }],
      },
    },
  },
  plugins: [],
};

export default config;
