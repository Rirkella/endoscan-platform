/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // EndoScan redesign palette — a sober "scientific instrument" system ported from the
        // validated prototype-v2 UX reference: deep green + warm neutrals, a terracotta SIGNAL
        // accent for attention (never alarming, never reassuring), amber for the provisional
        // experimental status, and an emerald `success` RESERVED for a future "validated" state
        // (deliberately unused in the live product today, so its absence carries information).
        ink: "#183039", // primary text (dark teal-slate)
        muted: "#5f7377", // secondary text / captions
        line: "#d6ded9", // hairline borders (green-grey)
        surface: "#eef1ef", // page background (warm off-white)
        card: "#ffffff",
        brand: "#17604e", // primary / interactive / links / active nav (deep green)
        "brand-dark": "#104c3e", // hover
        accent: "#8bd0b4", // mint highlight
        // SIGNAL = above-threshold / attention. Warm terracotta: it draws the eye without
        // implying hazard (not red) or safety (not green).
        signal: "#a8462f",
        "signal-bg": "#fbeee9",
        "signal-line": "#e6c3b7",
        // WARN = experimental / provisional (amber). Neither alarming nor reassuring.
        warn: "#8a6a22",
        "warn-bg": "#fdf6e3",
        "warn-line": "#e6d199",
        // SUCCESS = reserved for a future validated endpoint (unused in the live UI on purpose).
        success: "#1f7a5a",
      },
      boxShadow: {
        card: "0 12px 32px rgb(24 48 57 / 7%)",
      },
    },
  },
  plugins: [],
};
