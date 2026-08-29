# Autopsia del residuo

Cohorte: `exact, reconstructed`; canciones: **41**.
WER por bootstrap de canción: **5.78%** (CI95 3.52–8.42%).
Final p90: **750 ms** (CI95 511–1067 ms).

## type

| Bucket | Errores | % del total |
|---|---:|---:|
| substitution | 202 | 44.5% |
| deletion | 126 | 27.8% |
| insertion | 126 | 27.8% |

## position

| Bucket | Errores | % del total |
|---|---:|---:|
| line_interior | 234 | 51.5% |
| first_word | 136 | 30.0% |
| last_word | 84 | 18.5% |

## class

| Bucket | Errores | % del total |
|---|---:|---:|
| common_or_slang_unresolved | 398 | 87.7% |
| interjection | 48 | 10.6% |
| proper_name_candidate | 8 | 1.8% |

## language

| Bucket | Errores | % del total |
|---|---:|---:|
| es | 417 | 91.9% |
| en | 37 | 8.1% |

## repeat_context

| Bucket | Errores | % del total |
|---|---:|---:|
| repeated_line | 229 | 50.4% |
| unique_line | 225 | 49.6% |

## repeat_context_rate

| Contexto | Errores | Palabras | Tasa |
|---|---:|---:|---:|
| repeated_line | 229 | 3788 | 6.05% |
| unique_line | 225 | 4071 | 5.53% |

## Timing humano

Cambios: 1865 (observados 1197, derivados 668); hacia más tarde 687, hacia más temprano 1178.
Magnitud p50/p90: 2080/6800 ms.
