/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Designed-clean scientific-SaaS palette: calm slate + a restrained clinical blue.
        ink: "#0f172a",
        muted: "#475569",
        line: "#e2e8f0",
        surface: "#f8fafc",
        brand: "#1d4ed8",
      },
    },
  },
  plugins: [],
};
