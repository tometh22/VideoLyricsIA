import GenlyLogo from "./GenlyLogo";

export default function BrandLockup({ size = "md", className = "", title = "Genly", priority = false }) {
  return <GenlyLogo variant="color" size={size} className={className} title={title} priority={priority} />;
}
