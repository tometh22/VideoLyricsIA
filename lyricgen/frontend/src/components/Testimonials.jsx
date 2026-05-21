// Testimonials / artist quotes. Null-safe by design: renders NOTHING until real
// quotes are provided. We never ship fabricated testimonials. To populate: add
// { quote, name, role, avatar? } entries to QUOTES and the section appears.
const QUOTES = [
  // { quote: "...", name: "Artista", role: "Sello / proyecto", avatar: "/logos/clients/x.jpg" },
];

export default function Testimonials({ title, quotes = QUOTES }) {
  if (!quotes || quotes.length === 0) return null;
  return (
    <section className="relative z-10 py-20 px-6 max-w-5xl mx-auto">
      {title && <h2 className="text-3xl font-bold text-center mb-12">{title}</h2>}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-5">
        {quotes.map((q) => (
          <figure key={q.name} className="glass rounded-card p-6 flex flex-col gap-4">
            <blockquote className="text-sm text-gray-300 leading-relaxed flex-1">"{q.quote}"</blockquote>
            <figcaption className="flex items-center gap-3">
              {q.avatar && <img src={q.avatar} alt={q.name} className="w-9 h-9 rounded-full object-cover" />}
              <div>
                <p className="text-sm font-semibold text-white">{q.name}</p>
                {q.role && <p className="text-xs text-gray-500">{q.role}</p>}
              </div>
            </figcaption>
          </figure>
        ))}
      </div>
    </section>
  );
}
