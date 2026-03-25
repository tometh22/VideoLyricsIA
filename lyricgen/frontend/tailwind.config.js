/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        brand: "#8b7cf8",
        surface: "#0d0d14",
        "surface-light": "#1a1a2e",
      },
    },
  },
  plugins: [],
};
