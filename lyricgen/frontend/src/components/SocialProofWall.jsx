// Customer / partner logo wall ("Confían en GenLy"). Null-safe by design:
// renders NOTHING until real logos are provided. We never ship fake/aspirational
// customer logos — an empty wall is better than a fabricated one, especially
// when selling to labels. To populate: add { name, src, alt } entries to LOGOS
// (drop the files in /public/logos/clients/) and the section appears automatically.
const LOGOS = [
  // { name: "Sello X", src: "/logos/clients/sello-x.svg", alt: "Sello X" },
];

export default function SocialProofWall({ title, logos = LOGOS }) {
  if (!logos || logos.length === 0) return null;
  return (
    <section className="relative z-10 py-12 px-6 max-w-5xl mx-auto text-center">
      {title && <p className="text-xs text-gray-600 uppercase tracking-widest mb-8">{title}</p>}
      <div className="flex flex-wrap justify-center items-center gap-x-12 gap-y-6 opacity-60">
        {logos.map((l) => (
          <img key={l.name} src={l.src} alt={l.alt || l.name} className="h-7 w-auto object-contain grayscale hover:grayscale-0 transition" />
        ))}
      </div>
    </section>
  );
}
