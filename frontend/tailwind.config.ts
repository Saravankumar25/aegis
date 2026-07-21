import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        surface: "#0b1020",
        panel: "#131a2e",
        edge: "#243049",
      },
    },
  },
  plugins: [],
};

export default config;
