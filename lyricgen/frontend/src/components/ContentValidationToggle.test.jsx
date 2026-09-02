import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { I18nProvider } from "../i18n";
import ContentValidationToggle, {
  isUmgTenant,
  isUniversalAccount,
} from "./ContentValidationToggle";

function renderToggle(tenantId, billingGroup = null) {
  return render(
    <I18nProvider>
      <ContentValidationToggle
        tenantId={tenantId}
        billingGroup={billingGroup}
        value={true}
        onChange={vi.fn()}
        initialOpen={true}
      />
    </I18nProvider>,
  );
}

describe("ContentValidationToggle tenant policy", () => {
  it("renders Universal policy as a fixed notice with no bypass control", () => {
    renderToggle("universal-argentina");

    expect(screen.getByRole("note")).toHaveTextContent(
      "Protección Universal activa",
    );
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
  });

  it("keeps the explicit restricted/free choice for common users", () => {
    renderToggle("genly");

    expect(screen.getAllByRole("radio")).toHaveLength(2);
  });

  it("uses the Universal fixed policy for a country tenant in its billing group", () => {
    renderToggle("genly_country_team", " Universal_Music ");

    expect(screen.getByRole("note")).toHaveTextContent(
      "Protección Universal activa",
    );
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    expect(isUniversalAccount("genly_country_team", "universal_music")).toBe(true);
  });

  it("recognizes current and future Universal tenant spellings", () => {
    for (const tenant of [
      "umg",
      "universal",
      "universal_argentina",
      "universal-mexico",
    ]) {
      expect(isUmgTenant(tenant)).toBe(true);
    }
    expect(isUmgTenant("genly")).toBe(false);
  });
});
