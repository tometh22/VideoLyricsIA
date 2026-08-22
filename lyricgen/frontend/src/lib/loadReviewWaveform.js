export async function loadReviewWaveform({
  request,
  url,
  retries = 1,
  retryDelayMs = 400,
}) {
  if (typeof request !== "function" || !url) return null;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      const response = await request(url);
      if (response?.ok) {
        const payload = await response.json();
        return payload && Array.isArray(payload.peaks) ? payload : null;
      }
    } catch { /* bounded retry below */ }
    if (attempt < retries && retryDelayMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, retryDelayMs));
    }
  }
  return null;
}
