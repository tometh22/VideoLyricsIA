const SIZES = { sm: "w-[116px]", md: "w-[132px]", lg: "w-[200px]" };
const SOURCES = {
  color: "/brand/genly-logo-color.png?v=20260711b",
  white: "/brand/genly-logo-white.png?v=20260711b",
  dark: "/brand/genly-logo-dark.png?v=20260711b",
  icon: "/brand/genly-icon-color.png?v=20260711b",
};

export default function GenlyLogo({ variant = "color", size = "md", priority = false, className = "", title = "Genly" }) {
  const icon = variant === "icon";
  return (
    <img
      src={SOURCES[variant] || SOURCES.color}
      alt={title}
      width={icon ? 30 : size === "lg" ? 200 : size === "sm" ? 116 : 132}
      height={icon ? 30 : undefined}
      loading={priority ? "eager" : "lazy"}
      fetchPriority={priority ? "high" : "auto"}
      draggable={false}
      className={`${icon ? "h-[30px] w-[30px]" : `${SIZES[size] || SIZES.md} h-auto`} object-contain object-left select-none ${className}`}
    />
  );
}
