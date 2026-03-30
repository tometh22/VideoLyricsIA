/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        brand: {
          DEFAULT: "#7c5cfc",
          light: "#a78bfa",
          dark: "#5b3fd4",
          50: "#f3f0ff",
        },
        surface: {
          DEFAULT: "#09090f",
          1: "#111118",
          2: "#1a1a24",
          3: "#24243a",
        },
        accent: "#00d4aa",
      },
      fontFamily: {
        sans: ['"Inter"', "system-ui", "sans-serif"],
        display: ['"Inter"', "system-ui", "sans-serif"],
      },
      boxShadow: {
        glow: "0 0 40px rgba(124, 92, 252, 0.15)",
        "glow-lg": "0 0 80px rgba(124, 92, 252, 0.2)",
      },
      animation: {
        "pulse-slow": "pulse 3s ease-in-out infinite",
        "gradient-x": "gradient-x 6s ease infinite",
        "slide-up": "slide-up 0.5s ease-out",
        "fade-in": "fade-in 0.6s ease-out",
      },
      keyframes: {
        "gradient-x": {
          "0%, 100%": { backgroundPosition: "0% 50%" },
          "50%": { backgroundPosition: "100% 50%" },
        },
        "slide-up": {
          "0%": { transform: "translateY(20px)", opacity: 0 },
          "100%": { transform: "translateY(0)", opacity: 1 },
        },
        "fade-in": {
          "0%": { opacity: 0 },
          "100%": { opacity: 1 },
        },
      },
    },
  },
  plugins: [],
};
