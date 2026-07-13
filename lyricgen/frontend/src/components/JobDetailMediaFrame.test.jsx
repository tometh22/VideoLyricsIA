import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

describe("JobDetail media frame sizing contract", () => {
  it("uses intrinsic landscape/short ratios instead of full-width max-height", () => {
    const component = fs.readFileSync(path.resolve("src/components/JobDetail.jsx"), "utf8");
    const css = fs.readFileSync(path.resolve("src/index.css"), "utf8");

    expect(component).toContain("job-detail-media-frame--landscape");
    expect(component).toContain("job-detail-media-frame--short");
    expect(component).toContain('className="job-detail-media-video w-full h-full block object-contain');
    expect(component).toContain('imageFit="contain"');
    expect(component).not.toContain('activeTab === "short" ? "max-h-[600px]');
    expect(css).toMatch(/job-detail-media-frame--landscape[\s\S]*aspect-ratio:\s*16\s*\/\s*9/);
    expect(css).toMatch(/job-detail-media-frame--short[\s\S]*aspect-ratio:\s*9\s*\/\s*16/);
    expect(css).toContain("width: min(100%, 888.89px, 97.78vh)");
    expect(css).toContain("width: min(100%, 337.5px, 33.75vh)");
    expect(css).toMatch(/job-detail-media-video:fullscreen[\s\S]*width:\s*100vw[\s\S]*height:\s*100vh/);
  });
});
